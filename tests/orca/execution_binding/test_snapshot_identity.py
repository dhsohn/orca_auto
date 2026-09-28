"""Build, claim-time verification, crash recovery and cleanup read one rule set.

Each test feeds the same snapshot, descriptor, request or directory to every path
and asserts they agree, so a rule cannot drift in one of them.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.queue.types import QueueEntry
from orca_auto.orca import recovery_rebind, worker_execution
from orca_auto.orca.execution_binding import (
    STALE_RECOVERY_SNAPSHOT_ERROR,
    build_orca_execution_snapshot,
    cleanup_unowned_orca_execution_snapshot,
    orca_execution_snapshot_generation_dir,
    verify_orca_execution_snapshot,
)
from orca_auto.orca.execution_binding._inputs import _route_outputs
from orca_auto.orca.execution_binding._recovery import (
    _recovery_seed_plan,
    _validated_submitted_dependency_identity,
)
from orca_auto.orca.execution_binding._snapshot_identity import (
    dependency_role,
    validated_content_descriptor,
)
from orca_auto.orca.execution_binding._verify import _verify_source_descriptor
from orca_auto.orca.resource_directives import prepare_submission_resource_request
from tests.conftest import build_submitted_snapshot, make_app_cfg, write_fake_orca

_H2 = "2\nH2\nH 0 0 0\nH 0 0 0.74\n"
_INPUTS: dict[str, tuple[str, dict[str, str]]] = {
    "inline": ("! HF STO-3G Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", {}),
    "xyzfile": ("! HF STO-3G\n* xyzfile 0 1 geom.xyz\n", {"geom.xyz": _H2}),
    "same_stem_opt": ("! HF STO-3G Opt\n* xyzfile 0 1 job.xyz\n", {"job.xyz": _H2}),
    "moinp": (
        '! HF STO-3G MORead\n%moinp "guess.gbw"\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n',
        {"guess.gbw": "orbitals"},
    ),
    "neb_endpoints": (
        '! NEB-TS HF STO-3G\n%neb\n  Product "output.xyz"\n  TS = "guessTS.xyz"\nend\n'
        "* xyzfile 0 1 input.xyz\n",
        {
            "input.xyz": "1\nr\nH 0 0 0\n",
            "output.xyz": "1\np\nH 0 0 0\n",
            "guessTS.xyz": "1\nts\nH 0 0 0\n",
        },
    ),
    "hessian_pointcharges": (
        '! Opt\n%pointcharges "charges.pc"\n%geom\n  InHessName "initial.hess"\nend\n'
        "* xyzfile 0 1 input.xyz\n",
        {
            "input.xyz": "1\ninput\nH 0 0 0\n",
            "charges.pc": "0\n",
            "initial.hess": "$hessian\n1\n1.0\n$end\n",
        },
    ),
}


def _job(tmp_path: Path, name: str) -> tuple[Path, Path, Path]:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    text, files = _INPUTS[name]
    for file_name, content in files.items():
        (job_dir / file_name).write_text(content, encoding="utf-8")
    selected = job_dir / "job.inp"
    selected.write_text(text, encoding="utf-8")
    return job_dir, selected, write_fake_orca(tmp_path / "orca")


def _build(job_dir: Path, selected: Path, executable: Path, **kwargs: Any) -> dict[str, Any]:
    return build_submitted_snapshot(
        job_dir,
        selected,
        orca_executable=executable,
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        **kwargs,
    )


def _verify(job_dir: Path, snapshot: Any, row: dict[str, Any] | None = None) -> tuple[Path, str]:
    """Verify ``snapshot`` against the row values recorded with ``row`` (itself by default)."""
    recorded = snapshot if row is None else row
    return verify_orca_execution_snapshot(
        job_dir,
        snapshot,
        expected_selected_inp=recorded["selected_inp"],
        expected_source_selected_inp=recorded["source_selected_inp"],
        expected_selected_input_xyz=recorded["selected_input_xyz"],
        expected_resource_request=recorded["resource_request"],
    )


@pytest.mark.parametrize("name", list(_INPUTS))
def test_build_verify_and_recovery_agree_on_one_snapshot(tmp_path: Path, name: str) -> None:
    job_dir, selected, executable = _job(tmp_path, name)
    snapshot = _build(job_dir, selected, executable)

    # Claim-time verification accepts exactly what build recorded.
    verified_selected, _executable = _verify(job_dir, snapshot)
    assert str(verified_selected) == snapshot["selected_inp"]
    roles = [dependency_role(index) for index in range(len(snapshot["dependency_paths"]))]
    assert set(snapshot["source_inputs"]) == {"selected_source", *roles}
    assert set(snapshot["materialized_inputs"]) == set(roles)

    # Build reads the routes from the normalized source and verification from
    # the bound copy; the runtime-output rule sees the same answer in both.
    prepared = prepare_submission_resource_request(
        selected, selected.read_bytes(), default_max_cores=1, default_max_memory_gb=1
    )
    bound_text = Path(snapshot["selected_inp"]).read_text(encoding="utf-8")
    assert _route_outputs(prepared.normalized_payload.decode().splitlines()) == _route_outputs(
        bound_text.splitlines()
    )

    # Recovery reads the same descriptors back as the submitted identities.
    plan = _recovery_seed_plan(job_dir, snapshot)
    assert plan.submitted_dependency_identities == {
        path: validated_content_descriptor(snapshot["source_inputs"][role], error="unused")
        for path, role in zip(snapshot["dependency_paths"], roles, strict=True)
    }
    assert plan.selected_sha256 == snapshot["source_inputs"]["selected_source"]["sha256"]

    # A crashed generation rebuilds into a replacement that verifies again.
    generation = Path(snapshot["execution_dir"])
    (generation / "job.out").write_text("interrupted\n", encoding="utf-8")
    replacement = _build(job_dir, selected, executable, recovery_from=snapshot)
    _verify(job_dir, replacement)
    assert replacement["recovery"]["submitted_dependency_identities"] == (
        plan.submitted_dependency_identities
    )


_CURRENT = "current"


@pytest.mark.parametrize(
    "variant",
    [_CURRENT, "version2", "version_text", "retired_marker", "not_a_mapping"],
)
def test_build_verify_and_rebind_share_the_version_gate(tmp_path: Path, variant: str) -> None:
    job_dir, selected, executable = _job(tmp_path, "same_stem_opt")
    snapshot = _build(job_dir, selected, executable)
    candidate: Any = {
        _CURRENT: snapshot,
        "version2": {**snapshot, "version": 2},
        "version_text": {**snapshot, "version": "3"},
        "retired_marker": {**snapshot, "max_retries": 2},
        "not_a_mapping": ["execution_snapshot"],
    }[variant]

    outcomes: dict[str, str] = {}
    for path, call in (
        ("verify", lambda: _verify(job_dir, candidate, row=snapshot)),
        ("recovery", lambda: _build(job_dir, selected, executable, recovery_from=candidate)),
        ("rebind", lambda: recovery_rebind._validated_recovery_rebind_claim({}, candidate)),
    ):
        try:
            call()
        except ValueError as exc:
            outcomes[path] = str(exc)
        else:
            outcomes[path] = "accepted"
    version_refusals = {
        "verify": "Queue metadata 'execution_snapshot' has an unsupported version",
        "recovery": STALE_RECOVERY_SNAPSHOT_ERROR,
        "rebind": STALE_RECOVERY_SNAPSHOT_ERROR,
    }
    if variant == _CURRENT:
        assert outcomes == dict.fromkeys(version_refusals, "accepted")
    else:
        assert outcomes == version_refusals


_DIGEST = hashlib.sha256(b"x").hexdigest()


@pytest.mark.parametrize(
    ("sha256", "size_bytes"),
    [
        (_DIGEST, 1),
        (_DIGEST.upper(), 1),
        (f" {_DIGEST} ", 0),
        (_DIGEST[:-1], 1),
        ("z" * 64, 1),
        (_DIGEST, True),
        (_DIGEST, -1),
        (_DIGEST, "1"),
        (None, 1),
    ],
)
@pytest.mark.parametrize("path_form", ["canonical", "dotdot", "relative", "nul", "outside"])
def test_verify_and_recovery_accept_the_same_content_descriptors(
    tmp_path: Path, sha256: Any, size_bytes: Any, path_form: str
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    source_text = {
        "canonical": str(job_dir / "dep.xyz"),
        "dotdot": f"{job_dir}/sub/../dep.xyz",
        "relative": "job/dep.xyz",
        "nul": f"{job_dir}/dep\x00.xyz",
        "outside": str(tmp_path / "dep.xyz"),
    }[path_form]
    role = dependency_role(0)
    descriptor = {
        "role": role,
        "source_path": source_text,
        "sha256": sha256,
        "size_bytes": size_bytes,
    }

    def outcome(call: Any) -> Any:
        try:
            return call()
        except ValueError:
            return "refused"

    verified = outcome(
        lambda: _verify_source_descriptor(
            descriptor, role=role, expected_source=source_text, job_dir=job_dir
        )
    )
    recovered = outcome(
        lambda: _validated_submitted_dependency_identity(job_dir, source_text, descriptor)
    )

    assert (verified == "refused") == (recovered == "refused")
    if recovered != "refused":
        assert recovered == {"sha256": _DIGEST, "size_bytes": size_bytes}


@pytest.mark.parametrize("identity", ["job_dir_identity", "execution_dir_identity"])
def test_verify_recovery_and_cleanup_share_the_directory_identity(
    tmp_path: Path, identity: str
) -> None:
    job_dir, selected, executable = _job(tmp_path, "xyzfile")
    snapshot = _build(job_dir, selected, executable)
    moved = {**snapshot, identity: {**snapshot[identity], "inode": snapshot[identity]["inode"] + 1}}

    with pytest.raises(ValueError, match="identity changed"):
        _verify(job_dir, moved)
    with pytest.raises(ValueError, match="changed"):
        cleanup_unowned_orca_execution_snapshot(job_dir, moved)
    if identity == "execution_dir_identity":
        with pytest.raises(ValueError, match="^Queued ORCA generation directory identity changed$"):
            orca_execution_snapshot_generation_dir(job_dir, moved)
        with pytest.raises(ValueError, match="^Queued ORCA generation directory identity changed$"):
            _recovery_seed_plan(job_dir, moved)
    assert Path(snapshot["execution_dir"]).is_dir()
    _verify(job_dir, snapshot)


@pytest.mark.parametrize(
    ("request_value", "accepted"),
    [
        ({"max_cores": 1, "max_memory_gb": 1}, True),
        ({"max_cores": 0, "max_memory_gb": 1}, False),
        ({"max_cores": True, "max_memory_gb": 1}, False),
        ({"max_cores": 1.0, "max_memory_gb": 1}, False),
        ({"max_cores": 1}, False),
        ({"max_cores": 1, "max_memory_gb": 1, "gpus": 1}, False),
    ],
)
def test_build_and_the_worker_child_accept_the_same_resource_requests(
    tmp_path: Path, request_value: dict[str, Any], accepted: bool
) -> None:
    job_dir, selected, executable = _job(tmp_path, "inline")
    payload = selected.read_bytes()

    def build() -> Any:
        return build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request=request_value,
            orca_executable=executable,
            queue_root=job_dir,
            snapshot_intent_token="snapshot_intent-resources-0001",
            normalized_selected_payload=b"%pal nprocs 1 end\n%maxcore 1024\n" + payload,
            source_selected_payload=payload,
        )

    row = QueueEntry(
        queue_id="q-1",
        app_name="orca_auto_orca",
        task_id="task-1",
        task_kind="orca_run_inp",
        engine="orca",
        metadata={
            "reaction_dir": str(job_dir),
            "execution_snapshot": {},
            "resource_request": request_value,
        },
    )

    def claim() -> Any:
        return worker_execution._build_execution_context(
            make_app_cfg(tmp_path), row, admission_token=""
        )

    if accepted:
        build()
        # The worker child accepts the request and moves on to the snapshot.
        with pytest.raises(ValueError, match="unsupported version"):
            claim()
    else:
        with pytest.raises(
            ValueError, match="^ORCA execution snapshot resources must be positive integers$"
        ):
            build()
        with pytest.raises(ValueError, match="^Queued ORCA entry has no resource request$"):
            claim()
