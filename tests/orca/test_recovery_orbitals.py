"""Scientific binding stages independent of Linux queue/xattr ownership."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from orca_auto.orca.execution_binding import _capture
from orca_auto.orca.execution_binding._build import (
    _load_selected_snapshot_input,
    _materialize_snapshot_inputs,
    _plan_bound_dependencies,
)
from orca_auto.orca.execution_binding._models import _RecoveryPlan
from orca_auto.orca.execution_binding._recovery import _validated_recovery_checkpoint
from orca_auto.orca.execution_binding._rewrite import _write_bound_selected_snapshot
from orca_auto.orca.recovery_orbitals import (
    OrbitalReadiness,
    checkpoint_output_readiness,
    recovery_guess_policy,
)
from orca_auto.orca.resource_directives import prepare_submission_resource_request

_BANNER = b"                                 * O   R   C   A *\n"
_PRE = _BANNER + b"ORCA STARTUP CALCULATIONS\nORCA GUESS\nINITIAL GUESS: MODEL POTENTIAL\n"
_POST = _PRE + b"Finished Guess after   0.1 sec\n"
_GEOMETRY = "* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
_FIXTURES = Path(__file__).parents[1] / "fixtures" / "orca_6_1_1"


@pytest.mark.parametrize("guess", ["PModel", "PAtom", "Hueckel", "HCore"])
@pytest.mark.parametrize("form", ["! HF {g}", "%scf Guess {g} end", "% SCF\n GUESS = {g}\nEND"])
@pytest.mark.parametrize("lower", [False, True])
def test_fresh_guess_policy(tmp_path: Path, guess: str, form: str, lower: bool) -> None:
    source = form.format(g=guess)
    source = source.lower() if lower else source
    assert recovery_guess_policy(source.splitlines()) == ("explicit_fresh_guess")
    bound, materialized, decision = _bind_stages(tmp_path, source)
    assert all(line in bound for line in source.splitlines())
    assert "MORead" not in bound and materialized.recovery_checkpoint_private is None
    assert decision["reason"] == "explicit_fresh_guess"


@pytest.mark.parametrize(
    ("directives", "reason"),
    [
        ("! NoAutoStart", "autostart_disabled"),
        ("%scf AutoStart false end", "autostart_disabled"),
        ("% SCF\n AutoStart = # comment # FALSE\nEND", "autostart_disabled"),
        ("%scf AutoStart = 0 end", "autostart_disabled"),
        ("%scf AutoStart perhaps end", "ambiguous_scf_policy"),
        ("%scf Guess Mystery end", "explicit_fresh_guess"),
        ("%scf GuessMode CMatrix end", "explicit_state_control"),
        ("%scf Rotate {0, 1, 45} end end", "explicit_state_control"),
        ("! GuessMix", "explicit_state_control"),
        ("! DeltaSCF", "explicit_state_control"),
        ("%scf AlphaConf 0,1 BetaConf 1,0 end", "explicit_state_control"),
        ("%scf BrokenSym 1,1 end", "explicit_state_control"),
        ("%scf FlipSpin 0 FinalMs 0 end", "explicit_state_control"),
        ("%scf Occupation 1 end", "explicit_state_control"),
        ("%method Occupation 1 end", "explicit_state_control"),
        ("%scf UnknownPolicy true end", "unclassified_scf_policy"),
        ("%scf MaxIter", "ambiguous_scf_policy"),
        ("%tddft IRoot 1 NRoots 2 end", "explicit_state_control"),
        ("%scf MaxIter 50 end\n%scf MaxIter 60 end", "ambiguous_scf_policy"),
        ('! MORead\n%moinp "user.gbw"\n%scf AutoStart false end', "explicit_reuse"),
        ('% SCF Guess MORead MOInp = "user.gbw" AutoStart false end', "explicit_reuse"),
        ('%moinp "user.gbw"', "explicit_reuse"),
        ("# ! PModel\n! HF # GUESSMIX\n%scf MaxIter = 20 end", "automatic"),
        ("! HF # PAtom # STO-3G\n%scf AutoStart true MaxIter 10 end", "automatic"),
    ],
)
def test_policy_and_actual_bound_input(tmp_path: Path, directives: str, reason: str) -> None:
    assert recovery_guess_policy(directives.splitlines()) == reason
    bound, materialized, decision = _bind_stages(tmp_path, directives)
    assert decision["policy"] == reason
    # Only references may be rewritten. Scientific directives survive byte-for-byte.
    for line in directives.splitlines():
        assert line.replace('"user.gbw"', '"orca_auto_moinp_user.gbw"') in bound
    assert _GEOMETRY in bound
    if reason == "automatic":
        assert "MORead" in bound and '%moinp "orca_auto_moinp_probe.moinp.gbw"' in bound
        private = materialized.recovery_checkpoint_private
        assert private is not None
        assert stat.S_IMODE(private.stat().st_mode) == 0o400
    else:
        assert "probe.moinp.gbw" not in bound
        assert materialized.recovery_checkpoint_private is None
        if reason == "explicit_reuse":
            assert any(
                Path(item["path"]).read_bytes() == b"user-chain"
                for item in materialized.materialized_inputs.values()
            )
        else:
            assert "MORead" not in bound


def _bind_stages(tmp_path: Path, directives: str, *, payload: bytes = _POST):
    job = tmp_path.resolve()
    seed = job / "predecessor"
    seed.mkdir()
    selected = job / "probe.inp"
    source = ("! HF STO-3G\n" + directives + "\n" + _GEOMETRY).encode()
    selected.write_bytes(source)
    (seed / "probe.out").write_bytes(payload)
    (seed / "probe.gbw").write_bytes(b"captured-orbitals")
    user = job / "user.gbw"
    user.write_bytes(b"user-chain")
    recovery = _RecoveryPlan(
        "predecessor",
        seed,
        hashlib.sha256(source).hexdigest(),
        set(),
        None,
        {str(user): {"size_bytes": 10, "sha256": hashlib.sha256(b"user-chain").hexdigest()}},
    )
    prepared = prepare_submission_resource_request(
        selected, source, default_max_cores=1, default_max_memory_gb=1
    )
    loaded = _load_selected_snapshot_input(
        selected,
        normalized_selected_payload=prepared.normalized_payload,
        source_selected_payload=source,
        recovery=recovery,
        resource_request=prepared.resource_request,
    )
    plans, geometries, decision = _plan_bound_dependencies(job, selected, loaded, recovery=recovery)
    target = job / "replacement"
    target.mkdir()
    materialized = _materialize_snapshot_inputs(job, target, selected, loaded, plans, geometries)
    bound, identity = _write_bound_selected_snapshot(job, target, selected, loaded, materialized)
    assert hashlib.sha256(bound.read_bytes()).hexdigest() == identity["sha256"]
    return bound.read_text(), materialized, decision


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"arbitrary text\n",
        b"Finished Guess after 0.1 sec\n",
        _PRE,
        _POST[:-1],
        _POST + b"ORCA GUESS\n",
        _POST + _PRE,
        _POST + b"* GEOMETRY OPTIMIZATION CYCLE 2 *\n",
        _POST + b"ORCA STARTUP CALCULATIONS\n",
        _PRE + b"# Finished Guess after 0.1 sec\n",
        _PRE + b"| 1> Finished Guess after 0.1 sec\n",
        _PRE + b"INITIAL GUESS DONE (0.1 sec)\n",
        _PRE + b"ORCA LEAN-SCF\n",
        _POST + b"\xff\n",
        _PRE + b"INPUT FILE\nFinished Guess after 0.1 sec\n",
        _POST + b"INPUT FILE\n",
        _POST + b"\x00\n",
    ],
)
def test_ineligible_output(payload: bytes) -> None:
    assert not checkpoint_output_readiness(payload).eligible


@pytest.mark.parametrize("name", ["h2_sp.out", "water_opt_freq.out"])
def test_authentic_output_and_latest_phase(name: str) -> None:
    payload = (_FIXTURES / name).read_bytes()
    result = checkpoint_output_readiness(payload)
    assert result.eligible
    assert (
        payload.splitlines()[result.evidence_line - 1].strip().startswith(b"Finished Guess after")
    )
    # An earlier cycle must never qualify a later interrupted startup.
    for suffix in (_PRE, b"ORCA GUESS\n", b"* GEOMETRY OPTIMIZATION CYCLE 999 *\n"):
        assert not checkpoint_output_readiness(payload + suffix).eligible


@pytest.mark.parametrize(
    ("name", "old_guess", "final_energy", "startup", "guess_boundary", "new_guess"),
    [
        ("water_opt_freq", 1709, 1936, 1980, 2166, 2178),
        ("nh3_ts_irc", 1430, 1682, 1731, 1917, 1929),
    ],
)
def test_authentic_final_energy_requires_new_guess(
    name: str, old_guess: int, final_energy: int, startup: int, guess_boundary: int, new_guess: int
) -> None:
    lines = (_FIXTURES / f"{name}.out").read_bytes().splitlines(keepends=True)
    assert (
        recovery_guess_policy((_FIXTURES / f"{name}.inp").read_text().splitlines()) == "automatic"
    )
    before = checkpoint_output_readiness(b"".join(lines[: final_energy - 1]))
    assert before.eligible and before.evidence_line == old_guess
    # The final-energy heading precedes startup; that interval already needs a new GUESS.
    for end in (final_energy, startup - 1):
        assert checkpoint_output_readiness(b"".join(lines[:end])) == OrbitalReadiness(
            False, "no_completed_guess_after_boundary", final_energy
        )
    assert checkpoint_output_readiness(b"".join(lines[: new_guess - 1])) == OrbitalReadiness(
        False, "no_completed_guess_after_boundary", guess_boundary
    )
    assert checkpoint_output_readiness(b"".join(lines[:new_guess])) == OrbitalReadiness(
        True, "completed_guess", guess_boundary, new_guess
    )


@pytest.mark.parametrize(
    ("end", "boundary"),
    [
        (2552, 2552),  # IRC calculation starts.
        (2585, 2552),
        (2586, 2586),  # Numerical frequencies start.
        (2597, 2586),
        (2598, 2598),  # Reduced output is announced.
        (2599, 2598),  # Subsequent SCF output is redirected to nh3.lastscf.
        (2603, 2598),
        *((line, line) for line in range(2604, 2614)),  # Each displaced gradient starts.
        (2760, 2613),
        (2761, 2761),  # Forward IRC starts.
        (2779, 2761),
        (2780, 2780),  # Backward IRC starts.
        (None, 2780),  # Convergence and normal termination do not supply GUESS evidence.
    ],
)
def test_authentic_reduced_output_prefixes_are_ineligible(end: int | None, boundary: int) -> None:
    lines = (_FIXTURES / "nh3_ts_irc.out").read_bytes().splitlines(keepends=True)
    assert checkpoint_output_readiness(b"".join(lines[:2551])) == OrbitalReadiness(
        True, "completed_guess", 1917, 1929
    )
    assert checkpoint_output_readiness(b"".join(lines[:end])) == OrbitalReadiness(
        False, "no_completed_guess_after_boundary", boundary
    )


@pytest.mark.parametrize(
    ("boundary", "next_boundary"),
    [(2552, 2586), (2586, 2598), (2598, 2604), (2604, 2605), (2613, 2761), (2761, 2780)],
)
def test_fresh_guess_expires_at_next_reduced_phase(boundary: int, next_boundary: int) -> None:
    lines = (_FIXTURES / "nh3_ts_irc.out").read_bytes().splitlines(keepends=True)
    # Synthetic renewal only: the authentic reduced-output interval contains no GUESS completion.
    payload = b"".join(lines[:boundary]) + b"Finished Guess after   0.1 sec\n"
    assert checkpoint_output_readiness(payload) == OrbitalReadiness(
        True, "completed_guess", boundary, boundary + 1
    )
    payload += b"".join(lines[boundary:next_boundary])
    assert checkpoint_output_readiness(payload) == OrbitalReadiness(
        False, "no_completed_guess_after_boundary", next_boundary + 1
    )


@pytest.mark.parametrize(("name", "end"), [("water_opt_freq.out", 1936), ("nh3_ts_irc.out", 2604)])
def test_authentic_later_phase_suppresses_binding(tmp_path: Path, name: str, end: int) -> None:
    lines = (_FIXTURES / name).read_bytes().splitlines(keepends=True)
    payload = b"".join(lines[:end])
    bound, materialized, decision = _bind_stages(tmp_path, "", payload=payload)
    assert "MORead" not in bound and materialized.recovery_checkpoint_private is None
    assert decision["reason"] == "no_completed_guess_after_boundary"
    assert decision["output"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert decision["boundary_line"] == end and decision["evidence_line"] == 0


def _candidate(
    tmp_path: Path, checkpoint: bytes | None = b"orbitals", output: bytes | None = _POST
):
    seed = tmp_path / "predecessor"
    seed.mkdir()
    if checkpoint is not None:
        (seed / "probe.gbw").write_bytes(checkpoint)
    if output is not None:
        (seed / "probe.out").write_bytes(output)
    return dict(
        job_dir=tmp_path,
        seed_dir=seed,
        source_selected=tmp_path / "probe.inp",
        scanned_dependencies=[],
        estimated_source_bytes=0,
    )


@pytest.mark.parametrize("checkpoint", [None, b"", b"\0" * 64, b"x", b"arbitrary text"])
def test_unready_candidates_never_seed(tmp_path: Path, checkpoint: bytes | None) -> None:
    args = _candidate(tmp_path, checkpoint, _PRE)
    captured, decision = _validated_recovery_checkpoint(**args)
    assert captured is None and decision["status"] == "skipped"


@pytest.mark.parametrize(
    "defect",
    [
        "missing-output",
        "input-budget",
        "aggregate-budget",
        "collision",
        "zero-prefix",
        "output-budget",
    ],
)
def test_optional_seed_skip_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    args = _candidate(tmp_path)
    if defect == "missing-output":
        (args["seed_dir"] / "probe.out").unlink()
    elif defect == "input-budget":
        (args["seed_dir"] / "probe.gbw").write_bytes(b"x" * 1024)
        monkeypatch.setattr(_capture, "MAX_INPUT_SNAPSHOT_BYTES", 512)
    elif defect == "aggregate-budget":
        monkeypatch.setattr(_capture, "MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES", 3)
    elif defect == "collision":
        args["scanned_dependencies"] = [tmp_path / "probe.moinp.gbw"]
    elif defect == "zero-prefix":
        (args["seed_dir"] / "probe.gbw").write_bytes(b"\0" * 16 + b"x")
    else:
        monkeypatch.setattr(_capture, "MAX_INPUT_SNAPSHOT_BYTES", 64)
    captured, decision = _validated_recovery_checkpoint(**args)
    assert captured is None and decision["status"] == "skipped"


@pytest.mark.parametrize("name", ["probe.out", "probe.gbw"])
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "directory", "fifo"])
def test_unsafe_evidence_fails_closed(tmp_path: Path, name: str, unsafe: str) -> None:
    import os

    args = _candidate(tmp_path)
    path = args["seed_dir"] / name
    path.unlink()
    donor = tmp_path / "elsewhere"
    donor.write_bytes(_POST)
    if unsafe == "symlink":
        path.symlink_to(donor)
    elif unsafe == "hardlink":
        os.link(donor, path)
    elif unsafe == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(ValueError, match="symlink|regular file"):
        _validated_recovery_checkpoint(**args)


@pytest.mark.parametrize("failed_name", ["probe.out", "probe.gbw"])
def test_unstable_capture_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_name: str
) -> None:
    args = _candidate(tmp_path)
    original = _capture.capture

    def changing(path, **kwargs):
        if path.name == failed_name:
            raise ValueError("changed while it was read")
        return original(path, **kwargs)

    monkeypatch.setattr(_capture, "capture", changing)
    captured, decision = _validated_recovery_checkpoint(**args)
    assert captured is None
    assert decision["reason"].endswith("capture_failed")


def test_eligible_bytes_are_captured_once_and_materialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _capture.capture
    reads = []

    def change_after_capture(path, **kwargs):
        result = original(path, **kwargs)
        if path.name == "probe.gbw":
            reads.append(path)
            path.write_bytes(b"later-write")
        return result

    monkeypatch.setattr(_capture, "capture", change_after_capture)
    _, materialized, decision = _bind_stages(tmp_path, "")
    assert len(reads) == 1
    assert decision["status"] == "eligible"
    assert decision["output"]["sha256"] == hashlib.sha256(_POST).hexdigest()
    assert materialized.recovery_checkpoint_private.read_bytes() == b"captured-orbitals"


def test_never_searches_ancestor_or_private_seed(tmp_path: Path) -> None:
    args = _candidate(tmp_path, None)
    (tmp_path / "probe.gbw").write_bytes(b"ancestor")
    (args["seed_dir"] / "probe.moinp.gbw").write_bytes(b"earlier-seed")
    captured, decision = _validated_recovery_checkpoint(**args)
    assert captured is None and decision["reason"] == "checkpoint_absent"


def test_readiness_does_not_claim_to_validate_short_nonzero_gbw(tmp_path: Path) -> None:
    args = _candidate(tmp_path, b"x")
    captured, decision = _validated_recovery_checkpoint(**args)
    assert captured is not None and captured.payload == b"x"
    assert decision["reason"] == "completed_guess"
