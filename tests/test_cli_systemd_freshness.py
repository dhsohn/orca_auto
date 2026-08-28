from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto import _process_evidence, cli_systemd_freshness, cli_systemd_units


def _make_fake_git_checkout(source_root: Path) -> None:
    (source_root / ".git").mkdir(parents=True)


def _fake_proc_stat(pid: int, *, start_ticks: int = 123_456) -> bytes:
    fields_after_comm = ["S", *("0" for _ in range(18)), str(start_ticks)]
    return f"{pid} (orca worker) {' '.join(fields_after_comm)}\n".encode()


def _process_file_reader(
    import_sources: dict[int, Path], *, start_ticks: int = 123_456
) -> Callable[[str], bytes]:
    def _read(path: str) -> bytes:
        parts = Path(path).parts
        pid = int(parts[-2])
        if parts[-1] == "stat":
            return _fake_proc_stat(pid, start_ticks=start_ticks)
        assert parts[-1] == "environ"
        source = import_sources[pid]
        return f"{_process_evidence.PROCESS_IMPORT_SOURCE_ENV}={source}\0".encode()

    return _read


def test_collect_worker_staleness_uses_head_update_not_old_commit_timestamp(
    tmp_path: Path,
) -> None:
    # The commit object was created a day before this checkout deployed it. The
    # stale worker started after the commit timestamp but before the checkout
    # update, which the former `%ct` comparison incorrectly reported as fresh.
    head_update_epoch = 1_785_747_750
    head_commit_epoch = head_update_epoch - 86_400
    head_sha = "a" * 40
    source_root = tmp_path / "checkout"
    _make_fake_git_checkout(source_root)
    start_stamps = {
        "orca_auto-queue-worker@alice.service": "Mon 2026-08-03 08:02:30 UTC",
        "orca_auto-workflow-worker@alice.service": "Mon 2026-08-03 10:02:30 UTC",
    }
    main_pids = {
        "orca_auto-queue-worker@alice.service": "41",
        "orca_auto-workflow-worker@alice.service": "42",
    }

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            assert argv[1:3] == ["-C", str(source_root)]
            git_args = argv[3:]
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(source_root)
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            elif git_args == ["reflog", "-1", "--date=unix", "--format=%H%x00%gd"]:
                value = f"{head_sha}\0HEAD@{{{head_update_epoch}}}"
            else:
                assert git_args == ["show", "-s", "--format=%ct", head_sha]
                value = str(head_commit_epoch)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=ExecMainStartTimestamp", "--value"]:
            assert argv[4] == "--timestamp=utc"
            return subprocess.CompletedProcess(
                argv, 0, stdout=f"{start_stamps[argv[5]]}\n", stderr=""
            )
        assert argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]
        return subprocess.CompletedProcess(argv, 0, stdout=f"{main_pids[argv[4]]}\n", stderr="")

    statuses = (
        cli_systemd_units.ServiceUnitStatus(
            label="engines",
            unit="orca_auto-engine-workers@alice.target",
            active="active",
            enabled="enabled",
        ),
        cli_systemd_units.ServiceUnitStatus(
            label="worker",
            unit="orca_auto-queue-worker@alice.service",
            active="active",
            enabled="enabled",
        ),
        cli_systemd_units.ServiceUnitStatus(
            label="workflow",
            unit="orca_auto-workflow-worker@alice.service",
            active="active",
            enabled="disabled",
        ),
    )

    verdict = cli_systemd_freshness.collect_worker_staleness(
        statuses,
        run=_fake_run,
        source_root=source_root,
    )

    assert verdict is not None
    assert verdict["head_commit_epoch"] == head_commit_epoch
    assert verdict["head_update_epoch"] == head_update_epoch
    assert verdict["source_root"] == str(source_root)
    assert verdict["head_sha"] == head_sha
    assert verdict["undetermined"] == []
    # Only the pre-update worker service is stale; the fresh workflow worker and
    # the non-service engines target are not inspected as stale. In particular,
    # the stale worker started *after* the old commit object's timestamp.
    assert [entry["unit"] for entry in verdict["stale"]] == ["orca_auto-queue-worker@alice.service"]
    assert verdict["stale"][0]["pid"] == 41
    assert verdict["stale"][0]["started_epoch"] == head_update_epoch - 3600
    assert verdict["stale"][0]["started_epoch"] > head_commit_epoch
    assert verdict["stale"][0]["head_update_epoch"] == head_update_epoch
    assert {entry["source_root"] for entry in verdict["workers"]} == {str(source_root)}


def test_collect_worker_staleness_refreshes_shared_checkout_head_per_worker(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "checkout"
    _make_fake_git_checkout(source_root)
    old_sha = "a" * 40
    new_sha = "b" * 40
    old_update_epoch = 1_785_744_000
    worker_start_epoch = 1_785_747_600
    new_update_epoch = 1_785_751_200
    head_moved = False
    units = {
        "orca_auto-queue-worker@alice.service": "41",
        "orca_auto-workflow-worker@alice.service": "42",
    }

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal head_moved
        del check, stdout, stderr, text
        if argv[0] == "git":
            assert argv[1:3] == ["-C", str(source_root)]
            git_args = argv[3:]
            head_sha = new_sha if head_moved else old_sha
            update_epoch = new_update_epoch if head_moved else old_update_epoch
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(source_root)
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            elif git_args == ["reflog", "-1", "--date=unix", "--format=%H%x00%gd"]:
                value = f"{head_sha}\0HEAD@{{{update_epoch}}}"
            else:
                assert git_args == ["show", "-s", "--format=%ct", head_sha]
                value = str(update_epoch - 86_400)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=ExecMainStartTimestamp", "--value"]:
            assert argv[4] == "--timestamp=utc"
            if argv[5] == "orca_auto-workflow-worker@alice.service":
                head_moved = True
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="Mon 2026-08-03 09:00:00 UTC\n",
                stderr="",
            )
        assert argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]
        return subprocess.CompletedProcess(argv, 0, stdout=f"{units[argv[4]]}\n", stderr="")

    statuses = tuple(
        cli_systemd_units.ServiceUnitStatus(
            label=label,
            unit=unit,
            active="active",
            enabled="enabled",
        )
        for label, unit in (
            ("worker", "orca_auto-queue-worker@alice.service"),
            ("workflow", "orca_auto-workflow-worker@alice.service"),
        )
    )

    verdict = cli_systemd_freshness.collect_worker_staleness(
        statuses,
        run=_fake_run,
        source_root=source_root,
    )

    assert verdict is not None
    assert [entry["unit"] for entry in verdict["stale"]] == [
        "orca_auto-workflow-worker@alice.service"
    ]
    assert verdict["workers"][0]["head_sha"] == old_sha
    assert verdict["workers"][0]["started_epoch"] == worker_start_epoch
    assert verdict["workers"][1]["head_sha"] == new_sha
    assert verdict["workers"][1]["head_update_epoch"] == new_update_epoch


def test_collect_worker_staleness_observes_the_active_process_checkout(tmp_path: Path) -> None:
    unit_checkout = tmp_path / "unit-editable-checkout"
    import_source = unit_checkout / "src" / "orca_auto" / "_process_evidence.py"
    head_update_epoch = 1_785_747_750
    head_commit_epoch = head_update_epoch - 86_400
    head_sha = "b" * 40
    _make_fake_git_checkout(unit_checkout)
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# process evidence\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            # A regression to the status CLI's own editable checkout would not
            # match this process-observed root and fails the fixture immediately.
            assert argv[1:3] == ["-C", str(unit_checkout)]
            git_args = argv[3:]
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(unit_checkout)
            elif git_args == [
                "ls-files",
                "--error-unmatch",
                "--",
                "src/orca_auto/_process_evidence.py",
            ]:
                value = "src/orca_auto/_process_evidence.py"
            elif git_args == [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "src/orca_auto",
            ]:
                value = ""
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            elif git_args == ["reflog", "-1", "--date=unix", "--format=%H%x00%gd"]:
                value = f"{head_sha}\0HEAD@{{{head_update_epoch}}}"
            else:
                assert git_args == ["show", "-s", "--format=%ct", head_sha]
                value = str(head_commit_epoch)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="77\n", stderr="")
        assert argv[:4] == [
            "systemctl",
            "show",
            "--property=ExecMainStartTimestamp",
            "--value",
        ]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Mon 2026-08-03 08:02:30 UTC\n",
            stderr="",
        )

    statuses = (
        cli_systemd_units.ServiceUnitStatus(
            label="worker",
            unit=unit,
            active="active",
            enabled="enabled",
        ),
    )
    observed_proc_paths: list[str] = []
    read_process_file = _process_file_reader({77: import_source})

    def _read_process_file(path: str) -> bytes:
        observed_proc_paths.append(path)
        return read_process_file(path)

    verdict = cli_systemd_freshness.collect_worker_staleness(
        statuses,
        run=_fake_run,
        read_process_file=_read_process_file,
    )

    assert verdict is not None
    assert "/proc/77/environ" in observed_proc_paths
    assert all(not path.endswith("/cwd") for path in observed_proc_paths)
    assert verdict["source_root"] == str(unit_checkout)
    assert verdict["head_sha"] == head_sha
    assert verdict["workers"][0]["import_source"] == str(import_source)
    assert [entry["unit"] for entry in verdict["stale"]] == [unit]


def test_collect_worker_staleness_refuses_dirty_import_package(tmp_path: Path) -> None:
    unit_checkout = tmp_path / "dirty-editable-checkout"
    import_source = unit_checkout / "src" / "orca_auto" / "_process_evidence.py"
    head_update_epoch = 1_785_744_000
    head_sha = "c" * 40
    _make_fake_git_checkout(unit_checkout)
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# process evidence\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            assert argv[1:3] == ["-C", str(unit_checkout)]
            git_args = argv[3:]
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(unit_checkout)
            elif git_args == [
                "ls-files",
                "--error-unmatch",
                "--",
                "src/orca_auto/_process_evidence.py",
            ]:
                value = "src/orca_auto/_process_evidence.py"
            elif git_args == [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "src/orca_auto",
            ]:
                value = " M src/orca_auto/cli.py"
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            elif git_args == ["reflog", "-1", "--date=unix", "--format=%H%x00%gd"]:
                value = f"{head_sha}\0HEAD@{{{head_update_epoch}}}"
            else:
                assert git_args == ["show", "-s", "--format=%ct", head_sha]
                value = str(head_update_epoch - 86_400)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="79\n", stderr="")
        assert argv[:4] == [
            "systemctl",
            "show",
            "--property=ExecMainStartTimestamp",
            "--value",
        ]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Mon 2026-08-03 09:00:00 UTC\n",
            stderr="",
        )

    verdict = cli_systemd_freshness.collect_worker_staleness(
        (
            cli_systemd_units.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({79: import_source}),
    )

    assert verdict is not None
    assert verdict["workers"] == []
    assert verdict["stale"] == []
    assert verdict["undetermined"][0]["unit"] == unit
    assert "uncommitted source changes" in verdict["undetermined"][0]["detail"]


def test_collect_worker_staleness_returns_none_for_active_wheel_worker(tmp_path: Path) -> None:
    wheel_root = tmp_path / "wheel-runtime"
    import_source = (
        wheel_root / "lib" / "python3.13" / "site-packages" / "orca_auto" / "_process_evidence.py"
    )
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# installed wheel\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            pytest.fail("a wheel worker has no checkout to inspect with git")
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="91\n", stderr="")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Mon 2026-08-03 10:02:30 UTC\n",
            stderr="",
        )

    verdict = cli_systemd_freshness.collect_worker_staleness(
        (
            cli_systemd_units.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({91: import_source}),
    )

    assert verdict is None


def test_collect_worker_staleness_treats_wheel_inside_git_cwd_as_uncompared(
    tmp_path: Path,
) -> None:
    git_cwd = tmp_path / "git-working-directory"
    _make_fake_git_checkout(git_cwd)
    import_source = (
        git_cwd
        / ".venv"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "orca_auto"
        / "_process_evidence.py"
    )
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# installed wheel\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            assert argv[1:3] == ["-C", str(git_cwd)]
            if argv[3:] == ["rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(argv, 0, stdout=f"{git_cwd}\n", stderr="")
            assert argv[3:6] == ["ls-files", "--error-unmatch", "--"]
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="path is not tracked\n")
        assert argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]
        return subprocess.CompletedProcess(argv, 0, stdout="93\n", stderr="")

    verdict = cli_systemd_freshness.collect_worker_staleness(
        (
            cli_systemd_units.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({93: import_source}),
    )

    assert verdict is None


def test_collect_worker_staleness_fails_closed_when_process_identity_changes(
    tmp_path: Path,
) -> None:
    import_source = tmp_path / "wheel" / "site-packages" / "orca_auto" / "_process_evidence.py"
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# installed wheel\n", encoding="utf-8")
    observed_ticks = iter((111, 222))
    unit = "orca_auto-queue-worker@alice.service"

    def _read_process_file(path: str) -> bytes:
        if path.endswith("/stat"):
            return _fake_proc_stat(94, start_ticks=next(observed_ticks))
        return f"{_process_evidence.PROCESS_IMPORT_SOURCE_ENV}={import_source}\0".encode()

    def _fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="94\n", stderr="")

    verdict = cli_systemd_freshness.collect_worker_staleness(
        (
            cli_systemd_units.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_read_process_file,
    )

    assert verdict is not None
    assert verdict["workers"] == []
    assert verdict["stale"] == []
    assert verdict["undetermined"][0]["unit"] == unit
    assert "process identity changed" in verdict["undetermined"][0]["detail"]


def test_collect_worker_staleness_does_not_require_start_time_for_wheel_worker(
    tmp_path: Path,
) -> None:
    wheel_root = tmp_path / "wheel-runtime"
    import_source = (
        wheel_root / "lib" / "python3.13" / "site-packages" / "orca_auto" / "_process_evidence.py"
    )
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# installed wheel\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="91\n", stderr="")
        pytest.fail("a non-git worker has no checkout timestamp to compare")

    verdict = cli_systemd_freshness.collect_worker_staleness(
        (
            cli_systemd_units.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({91: import_source}),
    )

    assert verdict is None


def test_collect_worker_staleness_rechecks_pid_before_accepting_wheel_worker(
    tmp_path: Path,
) -> None:
    wheel_root = tmp_path / "wheel-runtime"
    import_source = (
        wheel_root / "lib" / "python3.13" / "site-packages" / "orca_auto" / "_process_evidence.py"
    )
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# installed wheel\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"
    observed_pids = iter(("91", "92"))

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=f"{next(observed_pids)}\n", stderr=""
            )
        pytest.fail("a raced non-git worker must not be inspected as stable")

    verdict = cli_systemd_freshness.collect_worker_staleness(
        (
            cli_systemd_units.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({91: import_source}),
    )

    assert verdict is not None
    assert verdict["stale"] == []
    assert verdict["uncompared"] == []
    assert verdict["undetermined"][0]["unit"] == unit
    assert verdict["undetermined"][0]["detail"] == ("main PID changed during freshness inspection")


def test_collect_worker_staleness_skips_wheel_worker_in_mixed_deployment(
    tmp_path: Path,
) -> None:
    git_root = tmp_path / "git-worker"
    wheel_root = tmp_path / "wheel-worker"
    git_import_source = git_root / "src" / "orca_auto" / "_process_evidence.py"
    wheel_import_source = (
        wheel_root / "lib" / "python3.13" / "site-packages" / "orca_auto" / "_process_evidence.py"
    )
    _make_fake_git_checkout(git_root)
    git_import_source.parent.mkdir(parents=True)
    git_import_source.write_text("# editable source\n", encoding="utf-8")
    wheel_import_source.parent.mkdir(parents=True)
    wheel_import_source.write_text("# installed wheel\n", encoding="utf-8")
    head_update_epoch = 1_785_747_750
    head_commit_epoch = head_update_epoch - 86_400
    head_sha = "d" * 40
    git_unit = "orca_auto-queue-worker@alice.service"
    wheel_unit = "orca_auto-workflow-worker@alice.service"
    pids = {git_unit: "101", wheel_unit: "102"}
    starts = {
        git_unit: "Mon 2026-08-03 08:02:30 UTC",
        wheel_unit: "Mon 2026-08-03 10:02:30 UTC",
    }

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            assert argv[1:3] == ["-C", str(git_root)]
            git_args = argv[3:]
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(git_root)
            elif git_args == [
                "ls-files",
                "--error-unmatch",
                "--",
                "src/orca_auto/_process_evidence.py",
            ]:
                value = "src/orca_auto/_process_evidence.py"
            elif git_args == [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "src/orca_auto",
            ]:
                value = ""
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            elif git_args == ["reflog", "-1", "--date=unix", "--format=%H%x00%gd"]:
                value = f"{head_sha}\0HEAD@{{{head_update_epoch}}}"
            else:
                assert git_args == ["show", "-s", "--format=%ct", head_sha]
                value = str(head_commit_epoch)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{pids[argv[4]]}\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=f"{starts[argv[5]]}\n", stderr="")

    verdict = cli_systemd_freshness.collect_worker_staleness(
        (
            cli_systemd_units.ServiceUnitStatus(
                label="worker",
                unit=git_unit,
                active="active",
                enabled="enabled",
            ),
            cli_systemd_units.ServiceUnitStatus(
                label="workflow",
                unit=wheel_unit,
                active="active",
                enabled="disabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({101: git_import_source, 102: wheel_import_source}),
    )

    assert verdict is not None
    assert [entry["unit"] for entry in verdict["stale"]] == [git_unit]
    assert verdict["undetermined"] == []
    assert verdict["uncompared"] == [
        {
            "label": "workflow",
            "unit": wheel_unit,
            "pid": 102,
            "source_root": str(wheel_import_source.parent),
            "import_source": str(wheel_import_source),
            "reason": "installed_distribution",
        }
    ]


def test_collect_worker_staleness_fails_closed_without_checkout_update_evidence(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "checkout-without-reflog"
    (source_root / ".git").mkdir(parents=True)
    head_sha = "c" * 40
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            git_args = argv[3:]
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(source_root)
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            else:
                assert git_args == [
                    "reflog",
                    "-1",
                    "--date=unix",
                    "--format=%H%x00%gd",
                ]
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout="",
                    stderr="fatal: no reflog for HEAD\n",
                )
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="88\n", stderr="")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Mon 2026-08-03 10:02:30 UTC\n",
            stderr="",
        )

    verdict = cli_systemd_freshness.collect_worker_staleness(
        (
            cli_systemd_units.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        source_root=source_root,
    )

    assert verdict is not None
    assert verdict["stale"] == []
    assert verdict["workers"] == []
    assert [entry["unit"] for entry in verdict["undetermined"]] == [unit]
    assert "cannot read checkout HEAD" in verdict["undetermined"][0]["detail"]
    assert "no reflog for HEAD" in verdict["undetermined"][0]["detail"]


def test_collect_worker_staleness_skips_inactive_workers_and_reports_unreadable_starts(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "checkout"
    (source_root / ".git").mkdir(parents=True)

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="41\n", stderr="")
        # An empty ExecMainStartTimestamp is what systemd reports when it has
        # no start record to offer.
        assert argv[:4] == [
            "systemctl",
            "show",
            "--property=ExecMainStartTimestamp",
            "--value",
        ]
        return subprocess.CompletedProcess(argv, 0, stdout="\n", stderr="")

    statuses = (
        cli_systemd_units.ServiceUnitStatus(
            label="worker",
            unit="orca_auto-queue-worker@alice.service",
            active="active",
            enabled="enabled",
        ),
        cli_systemd_units.ServiceUnitStatus(
            label="workflow",
            unit="orca_auto-workflow-worker@alice.service",
            active="inactive",
            enabled="disabled",
        ),
    )

    verdict = cli_systemd_freshness.collect_worker_staleness(
        statuses,
        run=_fake_run,
        source_root=source_root,
    )

    # The inactive workflow worker is not a running process to judge, while the
    # active worker with no readable start record must surface instead of passing.
    assert verdict is not None
    assert verdict["stale"] == []
    assert [entry["unit"] for entry in verdict["undetermined"]] == [
        "orca_auto-queue-worker@alice.service"
    ]
    assert "cannot read unit start time" in verdict["undetermined"][0]["detail"]


def test_collect_worker_staleness_returns_none_outside_a_git_checkout(tmp_path: Path) -> None:
    source_root = tmp_path / "wheel-install"
    source_root.mkdir()

    verdict = cli_systemd_freshness.collect_worker_staleness(
        (),
        run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
        source_root=source_root,
    )

    assert verdict is None


def test_collect_worker_staleness_fails_closed_on_unreadable_history(tmp_path: Path) -> None:
    source_root = tmp_path / "checkout"
    (source_root / ".git").mkdir(parents=True)

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        assert argv[0] == "git"
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: bad revision\n")

    verdict = cli_systemd_freshness.collect_worker_staleness(
        (),
        run=_fake_run,
        source_root=source_root,
    )

    assert verdict is not None
    assert verdict["head_commit_epoch"] is None
    assert verdict["stale"] == []
    assert "cannot read checkout HEAD" in verdict["undetermined"][0]["detail"]
    assert "fatal: bad revision" in verdict["undetermined"][0]["detail"]
