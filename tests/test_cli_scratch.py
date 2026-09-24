"""``orca_auto scratch list`` and ``scratch clear`` operate on the worker's scratch root."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orca_auto import cli as unified_cli
from orca_auto import cli_scratch
from orca_auto.core import engine_scratch as scratch_mod
from orca_auto.core.config import scratch as config_scratch
from orca_auto.core.engine_scratch import EngineScratchError, EngineScratchWorkspace
from orca_auto.orca.scratch import OrcaScratchPolicy
from tests.config_discovery_helpers import isolate_shared_config_discovery


@pytest.fixture(autouse=True)
def _isolate_shared_config_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_shared_config_discovery(monkeypatch, tmp_path)


@pytest.fixture
def scratch_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(scratch_mod, "_SCRATCH_ROOT_PARENT", shm)
    monkeypatch.setattr(config_scratch, "_SCRATCH_ROOT_PARENT", shm)
    monkeypatch.setattr(scratch_mod, "_linux_available_memory_bytes", lambda: 2**63)
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    fake_orca = tmp_path / "fake_orca"
    fake_orca.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_orca.chmod(0o755)
    scratch_root = shm / "orca_auto"
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        json.dumps(
            {
                "runs_root": str(runs_root),
                "orca": {
                    "runtime": {"scratch_root": str(scratch_root), "scratch_min_free_gb": 1},
                    "paths": {"orca_executable": str(fake_orca)},
                },
            }
        ),
        encoding="utf-8",
    )
    durable = runs_root / "sample" / "gen-1"
    durable.mkdir(parents=True)
    (durable / "sp.inp").write_text("! HF STO-3G SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    return {"config": config, "scratch_root": scratch_root, "durable": durable, "shm": shm}


def _policy(env: dict[str, Path]) -> OrcaScratchPolicy:
    return OrcaScratchPolicy(root=env["scratch_root"], min_free_bytes=1, max_task_memory_bytes=1)


def _manifest(durable: Path, **overrides: object) -> str:
    payload: dict[str, object] = {
        "schema_version": 2,
        "owner_pid": os.getpid(),
        "owner_process_start_ticks": scratch_mod.process_utils.current_process_start_ticks(),
        "owner_boot_id": scratch_mod.process_utils.linux_boot_id(proc_root=Path("/proc")),
        "durable_dir": str(durable.resolve()),
        "max_task_memory_bytes": 1,
    }
    payload.update(overrides)
    return json.dumps(payload, sort_keys=True) + "\n"


def _write_workspace(env: dict[str, Path], name: str, manifest: str) -> Path:
    root = scratch_mod._prepare_scratch_root(_policy(env))
    workspace = root / name
    workspace.mkdir()
    (workspace / scratch_mod.SCRATCH_MANIFEST_FILE_NAME).write_text(manifest, encoding="utf-8")
    return workspace


def _stale_manifest(durable: Path) -> str:
    return _manifest(durable, owner_pid=999999, owner_boot_id="old-boot")


def _main(*argv: str) -> int:
    return unified_cli.main(list(argv))


# --- parser -------------------------------------------------------------------------------


def test_build_parser_parses_scratch_commands() -> None:
    parser = unified_cli.build_parser()

    list_args = parser.parse_args(["scratch", "list", "--config", "/tmp/orca_auto.yaml", "--json"])
    assert list_args.command == "scratch"
    assert list_args.scratch_command == "list"
    assert list_args.orca_auto_config == "/tmp/orca_auto.yaml"
    assert list_args.json is True
    assert list_args.func is cli_scratch.cmd_scratch_list

    clear_args = parser.parse_args(["scratch", "clear", "attempt-1-ab", "--json"])
    assert clear_args.name == "attempt-1-ab"
    assert clear_args.all_stale is False
    assert clear_args.func is cli_scratch.cmd_scratch_clear

    all_args = parser.parse_args(["scratch", "clear", "--all-stale"])
    assert all_args.name is None
    assert all_args.all_stale is True


def test_scratch_examples_are_listed_in_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert _main() == 0
    out = capsys.readouterr().out
    assert "orca_auto scratch list" in out
    assert "orca_auto scratch clear" in out


# --- list ---------------------------------------------------------------------------------


def test_scratch_list_reports_an_absent_root(
    scratch_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _main("scratch", "list", "--config", str(scratch_env["config"])) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"root: {scratch_env['scratch_root']}" in captured.out
    assert "workspaces: 0" in captured.out
    assert "no scratch workspaces." in captured.out


def test_scratch_list_names_blocking_workspaces_and_exits_zero(
    scratch_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    durable = scratch_env["durable"]
    live = _write_workspace(scratch_env, "attempt-live", _manifest(durable))
    (live / "sp.out").write_bytes(b"x" * 2048)
    _write_workspace(scratch_env, "attempt-stale", _stale_manifest(durable))
    _write_workspace(scratch_env, "attempt-invalid", "nope\n")

    assert _main("scratch", "list", "--config", str(scratch_env["config"])) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "workspaces: 3" in captured.out
    assert "by state: invalid-manifest 1, live 1, stale 1" in captured.out
    assert f"  - attempt-live live pid {os.getpid()}" in captured.out
    assert str(durable.resolve()) in captured.out
    assert "  - attempt-stale stale pid 999999" in captured.out
    assert "  - attempt-invalid invalid-manifest" in captured.out
    assert "2 workspace(s) block every new scratch launch: attempt-invalid, attempt-stale" in (
        captured.out
    )
    assert "orca_auto scratch clear NAME" in captured.out


def test_scratch_list_json_payload(
    scratch_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    durable = scratch_env["durable"]
    stale = _write_workspace(scratch_env, "attempt-stale", _stale_manifest(durable))
    (stale / "partial.out").write_bytes(b"y" * 512)
    (durable / scratch_mod._PUBLICATION_JOURNAL_FILE_NAME).write_text(
        json.dumps({"schema_version": 1, "phase": "committed", "items": []}), encoding="utf-8"
    )

    assert _main("scratch", "list", "--config", str(scratch_env["config"]), "--json") == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["root"] == str(scratch_env["scratch_root"])
    assert payload["root_exists"] is True
    assert payload["workspace_count"] == 1
    assert payload["blocking_count"] == 1
    [row] = payload["workspaces"]
    assert row["name"] == "attempt-stale"
    assert row["path"] == str(stale)
    assert row["state"] == "stale"
    assert row["manifest_valid"] is True
    assert row["owner_pid"] == 999999
    assert row["owner_boot_id"] == "old-boot"
    assert row["durable_dir"] == str(durable.resolve())
    assert row["max_task_memory_bytes"] == 1
    assert row["size_bytes"] > 512
    assert row["size_walk_truncated"] is False
    assert row["blocks_launch"] is True
    assert "stale workspace" in row["detail"]
    assert row["publication_journal"]["phase"] == "committed"
    assert row["publication_journal"]["corrupt"] is False


def test_scratch_list_fails_on_an_unsafe_root(
    scratch_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    # A root that is not a directory cannot be opened fd-pinned; nothing is guessed.
    scratch_env["scratch_root"].write_text("", encoding="utf-8")

    assert _main("scratch", "list", "--config", str(scratch_env["config"])) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "Not a directory" in captured.err
    assert "unreadable or unsafe" in captured.err
    assert "Traceback" not in captured.err


def test_scratch_commands_require_a_configured_scratch_root(
    scratch_env: dict[str, Path], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    payload = json.loads(scratch_env["config"].read_text(encoding="utf-8"))
    payload["orca"]["runtime"] = {}
    config = tmp_path / "no-scratch.yaml"
    config.write_text(json.dumps(payload), encoding="utf-8")

    assert _main("scratch", "list", "--config", str(config)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "scratch_root is not configured" in captured.err

    assert _main("scratch", "clear", "--all-stale") == 1
    captured = capsys.readouterr()
    assert "shared config is not configured" in captured.err

    assert _main("scratch", "list", "--config", str(tmp_path / "absent.yaml")) == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "Traceback" not in captured.err


# --- clear --------------------------------------------------------------------------------


def test_scratch_clear_removes_a_stale_workspace_and_unblocks_create(
    scratch_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    durable = scratch_env["durable"]
    stale = _write_workspace(scratch_env, "attempt-stale", _stale_manifest(durable))
    (stale / "partial.out").write_bytes(b"evidence")
    policy = _policy(scratch_env)
    selected = durable / "sp.inp"
    with pytest.raises(EngineScratchError, match="stale workspace"):
        EngineScratchWorkspace.create(policy, selected)

    assert _main("scratch", "clear", "attempt-stale", "--config", str(scratch_env["config"])) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "removed: 1" in captured.out
    assert "  - attempt-stale stale" in captured.out
    assert "refused:" not in captured.out
    assert not stale.exists()
    workspace = EngineScratchWorkspace.create(policy, selected)
    assert workspace.path.is_dir()
    workspace.discard_unlaunched()


def test_scratch_clear_refuses_a_live_workspace(
    scratch_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    live = _write_workspace(scratch_env, "attempt-live", _manifest(scratch_env["durable"]))

    assert _main("scratch", "clear", "attempt-live", "--config", str(scratch_env["config"])) == 1

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "removed: 0" in captured.out
    assert "refused: 1" in captured.out
    assert f"  - attempt-live live workspace is live (owner pid {os.getpid()})" in captured.out
    assert live.is_dir()

    assert (
        _main("scratch", "clear", "attempt-live", "--config", str(scratch_env["config"]), "--json")
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed"] == []
    assert payload["removed_count"] == 0
    assert payload["refused"] == [
        {
            "name": "attempt-live",
            "state": "live",
            "reason": f"workspace is live (owner pid {os.getpid()})",
        }
    ]
    assert live.is_dir()


def test_scratch_clear_all_stale_keeps_live_and_reports_json(
    scratch_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    durable = scratch_env["durable"]
    live = _write_workspace(scratch_env, "attempt-live", _manifest(durable))
    stale = _write_workspace(scratch_env, "attempt-stale", _stale_manifest(durable))
    invalid = _write_workspace(scratch_env, "attempt-invalid", "{}")
    stray = durable / f"{scratch_mod._PUBLICATION_TEMP_PREFIX}{'f' * 32}.tmp"
    stray.write_bytes(b"half-copied")

    assert (
        _main("scratch", "clear", "--all-stale", "--config", str(scratch_env["config"]), "--json")
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["root"] == str(scratch_env["scratch_root"])
    assert payload["removed_count"] == 2
    assert payload["refused"] == []
    removed = {row["name"]: row for row in payload["removed"]}
    assert set(removed) == {"attempt-invalid", "attempt-stale"}
    assert removed["attempt-stale"]["state"] == "stale"
    assert removed["attempt-stale"]["durable_dir"] == str(durable.resolve())
    assert removed["attempt-invalid"]["state"] == "invalid-manifest"
    assert removed["attempt-invalid"]["durable_dir"] is None
    # The live peer publishes into the same generation, so its in-flight
    # publication temp file is left alone.
    assert removed["attempt-stale"]["removed_durable_entries"] == []
    assert "live workspace" in removed["attempt-stale"]["durable_note"]
    assert live.is_dir()
    assert not stale.exists()
    assert not invalid.exists()
    assert stray.read_bytes() == b"half-copied"


def test_scratch_clear_all_stale_cleans_unjournaled_publication_temps(
    scratch_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    durable = scratch_env["durable"]
    _write_workspace(scratch_env, "attempt-stale", _stale_manifest(durable))
    stray = durable / f"{scratch_mod._PUBLICATION_TEMP_PREFIX}{'a' * 32}.tmp"
    stray.write_bytes(b"half-copied")

    assert _main("scratch", "clear", "--all-stale", "--config", str(scratch_env["config"])) == 0

    out = capsys.readouterr().out
    assert f"removed publication temp: {durable.resolve()}/{stray.name}" in out
    assert not stray.exists()


def test_scratch_clear_exits_one_when_nothing_matches(
    scratch_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config = str(scratch_env["config"])

    assert _main("scratch", "clear", "attempt-missing", "--config", config) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no scratch workspace named 'attempt-missing'" in captured.err

    assert _main("scratch", "clear", "--all-stale", "--config", config) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "removed: 0" in captured.out
    assert "nothing to clear." in captured.out

    _write_workspace(scratch_env, "attempt-live", _manifest(scratch_env["durable"]))
    assert _main("scratch", "clear", "--all-stale", "--config", config, "--json") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "root": str(scratch_env["scratch_root"]),
        "removed_count": 0,
        "removed": [],
        "refused": [],
    }


def test_scratch_clear_requires_exactly_one_selector(
    scratch_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config = str(scratch_env["config"])

    assert _main("scratch", "clear", "--config", config) == 1
    assert "exactly one of NAME or --all-stale" in capsys.readouterr().err

    assert _main("scratch", "clear", "attempt-x", "--all-stale", "--config", config) == 1
    assert "exactly one of NAME or --all-stale" in capsys.readouterr().err


def test_scratch_clear_reports_a_workspace_that_turned_live_under_the_lock(
    scratch_env: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = scratch_env["durable"]
    stale = _write_workspace(scratch_env, "attempt-stale", _stale_manifest(durable))

    def refuse(_root: Path, name: str, **_kwargs: object) -> scratch_mod.ScratchWorkspaceRemoval:
        raise EngineScratchError(f"refusing to remove live engine scratch workspace: {name}")

    monkeypatch.setattr(cli_scratch, "remove_scratch_workspace", refuse)

    assert _main("scratch", "clear", "attempt-stale", "--config", str(scratch_env["config"])) == 1

    out = capsys.readouterr().out
    assert "refused: 1" in out
    assert "refusing to remove live" in out
    assert stale.is_dir()
