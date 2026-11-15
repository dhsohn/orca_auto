"""Native file-I/O/runner regressions; no ORCA or Linux launch gate is invoked."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from orca_auto.core.engine_scratch import EngineScratchCapacityError, EngineScratchError, _workspace
from orca_auto.core.utils import process as process_utils
from orca_auto.orca.engine_moinp import (
    engine_moinp_name,
    engine_moinp_sources,
    prepare_engine_moinp,
)
from orca_auto.orca.execution_binding import _build, verify_orca_execution_snapshot
from orca_auto.orca.execution_binding._verify import orca_execution_started_evidence
from orca_auto.orca.file_identity import file_content_identity
from orca_auto.orca.orca_runner import RunResult, WorkerShutdownInterrupt
from orca_auto.orca.scratch import OrcaScratchPolicy
from tests.conftest import build_submitted_snapshot, make_orca_runner, write_fake_orca

_GEOMETRY = "* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
_READY = "* O   R   C   A *\nORCA GUESS\nFinished Guess after 0.1 sec\n"
_FAKE_ENGINE = r"""
import hashlib, json, os, pathlib, re, stat, sys
inp = pathlib.Path(sys.argv[1])
name = re.search(r'(?i)moinp\s*=?\s*"([^"]+)"', inp.read_text()).group(1)
target = pathlib.Path(name)
source = pathlib.Path(name.removeprefix('orca_auto_moinp_'))
a, b = target.stat(), source.stat()
assert stat.S_IMODE(a.st_mode) == 0o600
assert stat.S_IMODE(b.st_mode) == 0o400
assert a.st_nlink == b.st_nlink == 1
assert (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino)
assert target.read_bytes() == source.read_bytes()
pathlib.Path('opened.json').write_text(json.dumps({
    'name': name, 'source': source.name,
    'sha256_before': hashlib.sha256(target.read_bytes()).hexdigest(),
    'copy_mode': oct(stat.S_IMODE(a.st_mode)), 'source_mode': oct(stat.S_IMODE(b.st_mode)),
}))
fd = os.open(target, os.O_RDWR | os.O_NOFOLLOW)
os.write(fd, b'engine-mutated')
os.fsync(fd)
os.close(fd)
pathlib.Path(inp.stem + '.gbw').write_bytes(b'new-output-checkpoint')
print('Fake engine consumed and modified the private copy')
"""


@pytest.fixture
def native_snapshot(monkeypatch):
    # Only Linux queue/xattr reservation is substituted. Build, capture, rewrite,
    # snapshot verification, staging, publication and the GBW I/O are real.
    counter = 0

    def reserve(job_dir, **kwargs):
        nonlocal counter
        counter += 1
        name = f"20261114-230000-{counter:08x}"
        generation = job_dir / name
        generation.mkdir(mode=0o700)
        info = generation.stat()
        return name, generation, (info.st_dev, info.st_ino)

    monkeypatch.setattr(_build, "_reserve_execution_generation", reserve)


def _case(tmp_path, form):
    job = tmp_path / "job"
    job.mkdir()
    source = job / "probe.inp"
    guess = {
        "top": '! MORead\n%moinp "donor.gbw"\n%scf AutoStart false end\n',
        "scf": '%scf Guess MORead MOInp = "donor.gbw" AutoStart false end\n',
        "automatic": "",
    }[form]
    source.write_text("! HF STO-3G SP\n" + guess + _GEOMETRY)
    donor = job / "donor.gbw"
    donor.write_bytes(b"original-submitted-orbitals")
    donor.chmod(0o400)
    executable = write_fake_orca(tmp_path / "fake-orca")
    snapshot = build_submitted_snapshot(
        job,
        source,
        orca_executable=executable,
        resource_request={"max_cores": 1, "max_memory_gb": 1},
    )
    if form == "automatic":
        previous = Path(snapshot["execution_dir"])
        (previous / "probe.out").write_text(_READY)
        donor = previous / "probe.gbw"
        donor.write_bytes(b"original-predecessor-orbitals")
        donor.chmod(0o600)
        snapshot = build_submitted_snapshot(
            job,
            source,
            orca_executable=executable,
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            recovery_from=snapshot,
        )
    return job, donor, snapshot, executable


def _verify(job, snapshot, **kwargs):
    return verify_orca_execution_snapshot(
        job,
        snapshot,
        expected_selected_inp=snapshot["selected_inp"],
        expected_source_selected_inp=snapshot["source_selected_inp"],
        expected_selected_input_xyz="",
        expected_resource_request=snapshot["resource_request"],
        **kwargs,
    )


def _fingerprint(path):
    info = path.stat()
    return path.read_bytes(), info.st_mode, info.st_mtime_ns, info.st_ino, info.st_nlink


def _policy(fake_shm, monkeypatch):
    # Native filesystems stand in for tmpfs; Linux RAM/boot probes are not a Mac gate.
    monkeypatch.setattr(_workspace, "_linux_available_memory_bytes", lambda: 2**63)
    monkeypatch.setattr(process_utils, "linux_boot_id", lambda: "native-file-test")
    monkeypatch.setattr(process_utils, "current_process_start_ticks", lambda: 123)
    return OrcaScratchPolicy(root=fake_shm / "pool", min_free_bytes=1, max_task_memory_bytes=1)


@pytest.mark.parametrize("form", ["top", "scf", "automatic"])
@pytest.mark.parametrize("scratch", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failed", "interrupted"])
def test_engine_can_modify_only_private_copy(
    tmp_path,
    monkeypatch,
    fake_shm,
    native_snapshot,
    form,
    scratch,
    outcome,
):
    job, donor, snapshot, executable = _case(tmp_path, form)
    generation = Path(snapshot["execution_dir"])
    inp = Path(snapshot["selected_inp"])
    copies = engine_moinp_sources(inp.read_text().splitlines())
    assert len(copies) == 1
    target, original = next(iter(copies.items()))
    bound = generation / original
    assert not (generation / target).exists()
    assert _GEOMETRY in inp.read_text()
    if form != "automatic":
        assert "AutoStart false" in inp.read_text()
    before = {
        p: _fingerprint(p) for p in (donor, inp, bound, Path(snapshot["source_selected_inp"]))
    }
    if form == "automatic":
        before.update({p: _fingerprint(p) for p in donor.parent.iterdir() if p.is_file()})
    metadata = copy.deepcopy(snapshot)
    policy = _policy(fake_shm, monkeypatch) if scratch else None
    runner = make_orca_runner(
        executable,
        generation,
        scratch_policy=policy,
        verify_snapshot=lambda **kw: _verify(job, snapshot, **kw),
    )

    def fake_launch(path, **kwargs):
        # Replace only the Linux-specific launch gate with a native subprocess.
        result = subprocess.run(
            [sys.executable, "-c", _FAKE_ENGINE, path.name],
            cwd=path.parent,
            capture_output=True,
            check=True,
        )
        path.with_suffix(".out").write_bytes(result.stdout)
        if outcome == "interrupted":
            raise WorkerShutdownInterrupt()
        return RunResult(
            str(path.with_suffix(".out")),
            int(outcome == "failed"),
            input_identity=file_content_identity(path),
        )

    monkeypatch.setattr(runner, "_run_in_place", fake_launch)
    runner.prepare(inp)
    if scratch:
        assert runner._prepared_workspace is not None
        # Reservation stages only immutable sources, not the mutable launch copy.
        workspace = runner._prepared_workspace.path
        assert not (workspace / target).exists()
        assert (workspace / original).read_bytes() == bound.read_bytes()
    if outcome == "interrupted":
        with pytest.raises(WorkerShutdownInterrupt):
            runner.run(inp)
    else:
        result = runner.run(inp)
        assert result.return_code == int(outcome == "failed")
        if scratch:
            assert result.scratch_provenance["publication_status"] == "committed"
            assert target in result.scratch_provenance["published_files"]
            assert original not in result.scratch_provenance["published_files"]
    _verify(job, snapshot, allow_runtime_outputs=True)
    assert snapshot == metadata
    assert all(_fingerprint(p) == value for p, value in before.items())
    assert (generation / target).read_bytes() != bound.read_bytes()
    assert (generation / "probe.gbw").read_bytes() == b"new-output-checkpoint"
    assert stat.S_IMODE((generation / target).stat().st_mode) == 0o600
    receipt = json.loads((generation / "opened.json").read_text())
    assert receipt["sha256_before"] == hashlib.sha256(before[bound][0]).hexdigest()
    assert receipt["name"] == target
    assert orca_execution_started_evidence(job, snapshot)
    if scratch:
        assert not workspace.exists()


@pytest.mark.parametrize("bad", ["symlink", "dangling", "hardlink", "existing", "fifo"])
def test_existing_engine_target_is_never_reused(tmp_path, bad):
    source = tmp_path / "donor.gbw"
    source.write_bytes(b"original")
    source.chmod(0o400)
    inp = tmp_path / "probe.inp"
    target = tmp_path / engine_moinp_name(source.name)
    inp.write_text(f'%moinp "{target.name}"\n')
    if bad in {"symlink", "dangling"}:
        target.symlink_to(source if bad == "symlink" else tmp_path / "missing")
    elif bad == "hardlink":
        os.link(source, target)
    elif bad == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(b"existing")
    before = _fingerprint(source)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises((OSError, ValueError)):
            prepare_engine_moinp(inp, directory_fd=fd)
    finally:
        os.close(fd)
    assert _fingerprint(source) == before


@pytest.mark.parametrize(
    "reference",
    [
        "../donor.gbw",
        "/tmp/donor.gbw",
        "orca_auto_moinp_../donor.gbw",
        "orca_auto_moinp_orca_auto_moinp_donor.gbw",
        "donor.gbw",
    ],
)
def test_unsafe_or_unbound_moinp_refused(reference):
    with pytest.raises(ValueError):
        engine_moinp_sources([f'%moinp "{reference}"'])


def test_scratch_reserves_space_for_private_copy(tmp_path, monkeypatch, fake_shm, native_snapshot):
    job, donor, snapshot, executable = _case(tmp_path, "top")
    policy = _policy(fake_shm, monkeypatch)
    inp = Path(snapshot["selected_inp"])
    # Enough for the immutable closure and reserve, but not a second GBW.
    free = inp.stat().st_size + donor.stat().st_size + policy.min_free_bytes
    monkeypatch.setattr(_workspace, "_filesystem_free_bytes", lambda fd: free)
    runner = make_orca_runner(
        executable,
        inp.parent,
        scratch_policy=policy,
        verify_snapshot=lambda **kw: _verify(job, snapshot, **kw),
    )
    with pytest.raises(EngineScratchCapacityError, match="insufficient free space"):
        runner.prepare(inp)
    assert runner._prepared_workspace is None
    assert not list(policy.root.glob("attempt-*"))
    assert not orca_execution_started_evidence(job, snapshot)


@pytest.mark.parametrize("change", ["bytes", "mode", "missing"])
@pytest.mark.parametrize("scratch", [False, True])
def test_immutable_source_still_checked_after_engine_run(
    tmp_path,
    monkeypatch,
    fake_shm,
    native_snapshot,
    change,
    scratch,
):
    job, _, snapshot, executable = _case(tmp_path, "top")
    inp = Path(snapshot["selected_inp"])
    runner = make_orca_runner(
        executable,
        inp.parent,
        scratch_policy=_policy(fake_shm, monkeypatch) if scratch else None,
        verify_snapshot=lambda **kw: _verify(job, snapshot, **kw),
    )

    def corrupt(path, **kwargs):
        source = path.parent / "donor.gbw"
        source.chmod(0o600)
        if change == "bytes":
            source.write_bytes(b"corrupted source")
            source.chmod(0o400)
        elif change == "missing":
            source.unlink()
        path.with_suffix(".out").write_text("fake engine\n")
        return RunResult(str(path.with_suffix(".out")), 0)

    monkeypatch.setattr(runner, "_run_in_place", corrupt)
    with pytest.raises((ValueError, EngineScratchError), match="immutable|corrupt|not a readable"):
        runner.run(inp)


@pytest.mark.parametrize(
    "defect",
    [
        "none",
        "no_open",
        "eacces",
        "live_mode",
        "live_hash",
        "missing_live",
        "durable_only",
        "no_phase",
        "wrong_cwd",
        "no_identity",
    ],
)
@pytest.mark.parametrize("use_scratch", [True, False])
def test_production_acceptance_requires_independent_copy_and_real_open(
    tmp_path,
    native_snapshot,
    defect,
    use_scratch,
):
    from tools import checkpoint_acceptance as harness

    _, _, snapshot, _ = _case(tmp_path, "top")
    inp = Path(snapshot["selected_inp"])
    fd = os.open(inp.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        prepare_engine_moinp(inp, directory_fd=fd)
    finally:
        os.close(fd)
    target = engine_moinp_name("donor.gbw")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    trace = f'123 openat(AT_FDCWD, "{target}", O_RDWR) = 16\n'
    if defect == "no_open":
        trace = trace.replace("O_RDWR", "O_RDONLY")
    if defect == "eacces":
        trace = trace.replace("= 16", "= -1 EACCES (Permission denied)")
    (evidence / "opens.123").write_text(trace)
    directory = inp.parent
    if use_scratch and defect != "durable_only":
        directory = tmp_path / "actual-scratch"
        directory.mkdir()
        for name in ("donor.gbw", target):
            shutil.copy2(inp.parent / name, directory / name)
    live = {
        target: {
            "immutable": harness.identity(directory / "donor.gbw"),
            "engine_copy": harness.identity(directory / target),
        }
    }
    if defect == "live_mode":
        live[target]["engine_copy"]["mode"] = "0o400"
    if defect == "live_hash":
        live[target]["immutable"]["sha256"] = "0" * 64
    capture = {
        "directory": str(directory),
        "execution_dir": str(inp.parent),
        "scratch_workspace": str(directory) if directory != inp.parent else None,
        "engine_cwd": str(directory) if defect != "wrong_cwd" else "/unrelated",
        "engine": {
            "engine_process_state": "active",
            "engine_pid": None if defect == "no_identity" else 123,
            "engine_process_start_ticks": 456,
            "engine_process_boot_id": "boot",
        },
        "engine_alive_before": True,
        "engine_alive_after": defect != "no_phase",
        "copies": live,
    }
    observation = {
        "evidence_dir": str(evidence),
        "engine_copies": {str(directory): capture} if defect != "missing_live" else {},
    }
    if defect == "none" or (defect == "durable_only" and not use_scratch):
        assert (
            harness.production_copy_receipt(snapshot, observation, use_scratch=use_scratch)[target][
                "immutable"
            ]["mode"]
            == "0o400"
        )
    else:
        with pytest.raises(ValueError):
            harness.production_copy_receipt(snapshot, observation, use_scratch=use_scratch)


def test_old_snapshot_cannot_launch_or_create_copy(tmp_path, native_snapshot, monkeypatch):
    job, _, snapshot, executable = _case(tmp_path, "top")
    inp = Path(snapshot["selected_inp"])
    snapshot["version"] = 3
    runner = make_orca_runner(
        executable, inp.parent, verify_snapshot=lambda **kw: _verify(job, snapshot, **kw)
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("old snapshot must not reach the engine")

    monkeypatch.setattr(runner, "_run_in_place", forbidden)
    with pytest.raises(ValueError, match="unsupported version"):
        runner.run(inp)
    assert not (inp.parent / engine_moinp_name("donor.gbw")).exists()


def test_moinp_source_symlink_cannot_be_followed(tmp_path):
    outside = tmp_path / "outside.gbw"
    outside.write_bytes(b"untouchable")
    outside.chmod(0o400)
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    (engine_dir / "donor.gbw").symlink_to(outside)
    inp = engine_dir / "probe.inp"
    target = engine_moinp_name("donor.gbw")
    inp.write_text(f'%moinp "{target}"\n')
    before = _fingerprint(outside)
    fd = os.open(engine_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises((ValueError, OSError)):
            prepare_engine_moinp(inp, directory_fd=fd)
    finally:
        os.close(fd)
    assert not (engine_dir / target).exists()
    assert _fingerprint(outside) == before
