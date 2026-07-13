from __future__ import annotations

import errno
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig
from orca_auto.core.config.schema import CommonRuntimeConfig
from orca_auto.core.queue.engine.input_snapshot import SNAPSHOT_DIR_NAME
from orca_auto.flow.engines.crest import job_inputs as _helpers
from orca_auto.flow.engines.crest import submission as crest_submission
from tests.engine_artifact_helpers import (
    engine_payload as _engine_payload,
)
from tests.engine_artifact_helpers import (
    input_payload as _input_payload,
)
from tests.engine_artifact_helpers import (
    job as _job,
)
from tests.engine_artifact_helpers import (
    resources as _resources,
)
from tests.engine_artifact_helpers import (
    status as _status,
)
from tests.engine_artifact_helpers import (
    timestamps as _timestamps,
)


def _cfg(tmp_path: Path) -> AppConfig:
    allowed_root = tmp_path / "allowed_root"
    organized_root = tmp_path / "organized_root"
    allowed_root.mkdir()
    organized_root.mkdir()
    return AppConfig(
        runtime=CommonRuntimeConfig(
            allowed_root=str(allowed_root),
        )
    )


def _write_xyz(path: Path, comment: str = "test") -> None:
    path.write_text(f"1\n{comment}\nH 0.0 0.0 0.0\n", encoding="utf-8")


def _set_mtime(path: Path, *, seconds: int) -> None:
    stamp_ns = seconds * 1_000_000_000
    os.utime(path, ns=(stamp_ns, stamp_ns))


def test_load_job_manifest_returns_empty_dict_when_manifest_is_missing(tmp_path: Path) -> None:
    assert _helpers.load_job_manifest(tmp_path) == {}


def test_submission_failure_removes_its_snapshot_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    namespace: Path | None = None

    def fail_after_namespace(*args: object, **kwargs: object) -> object:
        nonlocal namespace
        snapshot_namespace = kwargs["snapshot_namespace"]
        assert isinstance(snapshot_namespace, str)
        namespace = job_dir / SNAPSHOT_DIR_NAME / snapshot_namespace
        (namespace / "partial").write_text("partial", encoding="utf-8")
        raise OSError(errno.EIO, "simulated snapshot fsync failure")

    monkeypatch.setattr(crest_submission, "new_job_id", lambda: "crest-fsync-failure")
    monkeypatch.setattr(crest_submission, "_build_submission_impl", fail_after_namespace)

    with pytest.raises(OSError, match="simulated snapshot fsync failure"):
        crest_submission._build_submission(object(), job_dir, {}, object())

    assert namespace is not None
    assert not namespace.exists()


def test_submission_validates_electronic_state_on_selected_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_root = tmp_path / "allowed"
    job_dir = allowed_root / "job"
    job_dir.mkdir(parents=True)
    selected = job_dir / "h.xyz"
    selected.write_text("1\ndoublet\nH 0 0 0\n", encoding="utf-8")
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    crest_executable = executable_dir / "crest"
    xtb_executable = executable_dir / "xtb"
    for executable in (crest_executable, xtb_executable):
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(allowed_root=str(allowed_root)),
        resources=SimpleNamespace(max_cores_per_task=4, max_memory_gb_per_task=8),
        paths=SimpleNamespace(
            crest_executable=str(crest_executable),
            xtb_executable=str(xtb_executable),
        ),
    )
    monkeypatch.setattr(crest_submission, "new_job_id", lambda: "crest-electronic-state")

    with pytest.raises(ValueError, match="parity"):
        crest_submission._build_submission(
            cfg,
            job_dir,
            {"input_xyz": "h.xyz", "charge": 0, "uhf": 0},
            SimpleNamespace(priority=10),
        )

    assert not (job_dir / SNAPSHOT_DIR_NAME / "crest-electronic-state").exists()


def test_load_job_manifest_reads_yaml_mapping(tmp_path: Path) -> None:
    manifest_path = tmp_path / _helpers.MANIFEST_FILE_NAME
    manifest_path.write_text("mode: nci\ninput_xyz: picked.xyz\n", encoding="utf-8")

    manifest = _helpers.load_job_manifest(tmp_path)

    assert manifest == {"mode": "nci", "input_xyz": "picked.xyz"}


def test_load_job_manifest_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    manifest_path = tmp_path / _helpers.MANIFEST_FILE_NAME
    manifest_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid CREST job manifest"):
        _helpers.load_job_manifest(tmp_path)


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ({}, "standard"),
        ({"mode": " NCI "}, "nci"),
    ],
)
def test_job_mode_normalizes_mode_values(manifest: dict[str, object], expected: str) -> None:
    assert _helpers.job_mode(manifest) == expected


def test_job_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="standard.*nci"):
        _helpers.job_mode({"mode": "fast"})


def test_select_latest_xyz_prefers_non_generated_candidates(tmp_path: Path) -> None:
    generated = tmp_path / "crest_best.xyz"
    selected = tmp_path / "molecule.xyz"
    _write_xyz(generated, "generated")
    _write_xyz(selected, "selected")
    _set_mtime(selected, seconds=10)
    _set_mtime(generated, seconds=20)

    latest = _helpers.select_latest_xyz(tmp_path)

    assert latest == selected


def test_select_latest_xyz_falls_back_to_newest_generated_candidate(tmp_path: Path) -> None:
    older = tmp_path / "coord.xyz"
    newer = tmp_path / "struc_final.xyz"
    _write_xyz(older, "older")
    _write_xyz(newer, "newer")
    _set_mtime(older, seconds=10)
    _set_mtime(newer, seconds=20)

    latest = _helpers.select_latest_xyz(tmp_path)

    assert latest == newer


def test_select_latest_xyz_raises_when_directory_has_no_xyz_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"No \.xyz file found"):
        _helpers.select_latest_xyz(tmp_path)


def test_select_input_xyz_returns_manifest_selected_file(tmp_path: Path) -> None:
    selected = tmp_path / "nested" / "chosen.xyz"
    selected.parent.mkdir()
    _write_xyz(selected, "chosen")

    resolved = _helpers.select_input_xyz(tmp_path, {"input_xyz": "nested/chosen.xyz"})

    assert resolved == selected.resolve()


def test_select_input_xyz_rejects_missing_manifest_selected_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Manifest input_xyz not found"):
        _helpers.select_input_xyz(tmp_path, {"input_xyz": "missing.xyz"})


def test_select_input_xyz_rejects_non_xyz_manifest_selected_file(tmp_path: Path) -> None:
    not_xyz = tmp_path / "chosen.txt"
    not_xyz.write_text("not xyz\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"must point to a \.xyz file"):
        _helpers.select_input_xyz(tmp_path, {"input_xyz": "chosen.txt"})


def test_select_input_xyz_uses_latest_xyz_when_manifest_has_no_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = tmp_path / "fallback.xyz"
    _write_xyz(fallback, "fallback")
    called_with: list[Path] = []

    def fake_select_latest_xyz(job_dir: Path) -> Path:
        called_with.append(job_dir)
        return fallback

    monkeypatch.setattr(_helpers, "select_latest_xyz", fake_select_latest_xyz)

    selected = _helpers.select_input_xyz(tmp_path, {})

    assert selected == fallback
    assert called_with == [tmp_path]


def test_queued_state_payload_copies_resource_request_and_sets_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_helpers, "now_utc_iso", lambda: "2026-04-19T00:00:00+00:00")
    resource_request = {"max_cores": 8, "max_memory_gb": 32}
    job_dir = tmp_path / "job"
    selected_xyz = job_dir / "input.xyz"

    payload = _helpers.queued_state_payload(
        job_id="crest-123",
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        mode="nci",
        molecule_key="mol-1",
        resource_request=resource_request,
    )

    assert payload["schema_version"] == 1
    assert payload["engine"] == "crest"
    assert _job(payload)["id"] == "crest-123"
    assert _job(payload)["dir"] == str(job_dir)
    assert _input_payload(payload)["selected_xyz_path"] == str(selected_xyz)
    assert _engine_payload(payload)["molecule_key"] == "mol-1"
    assert _engine_payload(payload)["mode"] == "nci"
    assert _status(payload)["state"] == "queued"
    assert _timestamps(payload)["created_at"] == "2026-04-19T00:00:00+00:00"
    assert _timestamps(payload)["updated_at"] == "2026-04-19T00:00:00+00:00"
    assert _resources(payload)["request"] == {"max_cores": 8, "max_memory_gb": 32}
    assert _resources(payload)["actual"] == {"max_cores": 8, "max_memory_gb": 32}
    assert _resources(payload)["request"] is not resource_request
    assert _resources(payload)["actual"] is not resource_request


def test_resolve_job_dir_accepts_job_under_allowed_root(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    job_dir = Path(cfg.runtime.allowed_root) / "job-42"
    job_dir.mkdir()

    resolved = _helpers.resolve_job_dir(cfg, str(job_dir))

    assert resolved == job_dir.resolve()


def test_new_job_id_uses_crest_prefix_and_timestamp_shape() -> None:
    job_id = _helpers.new_job_id()

    assert re.fullmatch(r"crest_\d{8}_\d{6}_[0-9a-f]{32}", job_id)
