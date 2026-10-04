from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.input_validation import validate_unambiguous_orca_directives
from orca_auto.orca.resource_directives import (
    maxcore_mb_per_core,
    prepare_submission_resource_request,
    read_maxcore,
    read_nprocs,
    resource_request_from_lines,
)


def _write_inp(tmp_path: Path, text: str) -> Path:
    inp = tmp_path / "rxn.inp"
    inp.write_text(text, encoding="utf-8")
    return inp


def test_prepare_submission_resource_request_rejects_invalid_utf8(tmp_path: Path) -> None:
    inp = tmp_path / "rxn.inp"
    payload = b"! Opt\n%pal nprocs 2 end\n%maxcore 1024\n\xff\n"
    inp.write_bytes(payload)

    with pytest.raises(ValueError, match="UTF-8"):
        prepare_submission_resource_request(
            inp, inp.read_bytes(), default_max_cores=2, default_max_memory_gb=2
        )

    assert inp.read_bytes() == payload


def test_prepare_submission_resource_request_injects_missing_directives(tmp_path: Path) -> None:
    source = "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    inp = _write_inp(tmp_path, source)

    prepared = prepare_submission_resource_request(
        inp, inp.read_bytes(), default_max_cores=8, default_max_memory_gb=32
    )

    assert prepared.resource_request == {"max_cores": 8, "max_memory_gb": 32}
    assert prepared.actions == ("pal_nprocs_injected", "maxcore_injected")
    assert inp.read_text(encoding="utf-8") == source
    # Independent: maxcore_mb_per_core(32,8)=32768//8=4096; %pal block inserted
    # before geometry with a trailing blank line; %maxcore inserted after route.
    assert prepared.normalized_payload == (
        b"! Opt\n%maxcore 4096\n%pal\n  nprocs 8\nend\n\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    )


def test_prepare_submission_resource_request_preserves_existing_nprocs(tmp_path: Path) -> None:
    source = "! Opt\n%pal\n  nprocs 12\nend\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    inp = _write_inp(tmp_path, source)

    prepared = prepare_submission_resource_request(
        inp, inp.read_bytes(), default_max_cores=8, default_max_memory_gb=32
    )

    # Independent: maxcore_mb_per_core(32,12)=32768//12=2730;
    # total_memory_gb=ceil(12*2730/1024)=ceil(31.992...)=32.
    assert prepared.resource_request == {"max_cores": 12, "max_memory_gb": 32}
    assert prepared.actions == ("maxcore_injected",)
    assert prepared.normalized_payload == (
        b"! Opt\n%maxcore 2730\n%pal\n  nprocs 12\nend\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    )


def test_prepare_submission_resource_request_honors_pal_route_shorthand(tmp_path: Path) -> None:
    # "! Opt PAL4" already requests 4 processes via ORCA's route shorthand, so
    # no conflicting %pal nprocs block should be injected and the resource
    # request must reflect 4 cores (not the default_max_cores).
    source = "! Opt PAL4\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    inp = _write_inp(tmp_path, source)

    prepared = prepare_submission_resource_request(
        inp, inp.read_bytes(), default_max_cores=8, default_max_memory_gb=32
    )

    # Independent: maxcore_mb_per_core(32,4)=32768//4=8192;
    # total_memory_gb=ceil(4*8192/1024)=32.
    assert prepared.resource_request == {"max_cores": 4, "max_memory_gb": 32}
    assert prepared.actions == ("maxcore_injected",)
    assert prepared.normalized_payload == (
        b"! Opt PAL4\n%maxcore 8192\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    )


@pytest.mark.parametrize(
    "pal",
    [
        "%pal nprocs = 16 end",
        "%pal nprocs=16 end",
        "%pal\n  nprocs = 16\nend",
        "%pal\n  nprocs =16\nend",
        "%pal\n  nprocs 16;\nend",
    ],
)
def test_prepare_submission_resource_request_honors_nprocs_with_optional_equals(
    tmp_path: Path, pal: str
) -> None:
    # ORCA accepts an optional "=" between a block key and its value; the
    # directive must be honored, not overwritten with the configured default.
    source = f"! Opt\n{pal}\n%maxcore 2000\n* xyzfile 0 1 g.xyz\n"
    inp = _write_inp(tmp_path, source)

    prepared = prepare_submission_resource_request(
        inp, inp.read_bytes(), default_max_cores=4, default_max_memory_gb=8
    )

    assert read_nprocs(source.splitlines()) == 16
    assert prepared.actions == ()
    assert prepared.resource_request == {"max_cores": 16, "max_memory_gb": 32}
    assert prepared.normalized_payload.decode("utf-8") == source


def test_resource_request_from_lines_uses_inp_values() -> None:
    lines = "! Opt\n%pal\n  nprocs 6\nend\n%maxcore 3072\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"

    assert resource_request_from_lines(lines.splitlines()) == {"max_cores": 6, "max_memory_gb": 18}


def test_resource_readers_use_maximum() -> None:
    lines = [
        "%maxcore 1000",
        "# hidden # %maxcore 999999",
        "! SP PAL4 PAL8",
        "* xyz 0 1",
        "H 0 0 0",
        "*",
    ]

    assert read_maxcore(lines) == 999999
    assert read_nprocs(lines) == 8


@pytest.mark.parametrize(
    ("line", "value"),
    [("%maxcore 512", 512), ("% maxcore 512", 512), ("%maxcore 512 # MB", 512)],
)
def test_maxcore_value_is_read_only_after_whitespace(line: str, value: int) -> None:
    assert read_maxcore([line]) == value


@pytest.mark.parametrize("line", ["%maxcore", "%maxcore =512", "%maxcore-5", "%maxcore abc"])
def test_maxcore_directive_without_a_value_is_counted_but_not_read(line: str) -> None:
    assert read_maxcore([line]) is None
    with pytest.raises(ValueError, match="ambiguous duplicate ORCA directives: %maxcore"):
        validate_unambiguous_orca_directives(["%maxcore 512", line], label="job.inp")


# ---------------------------------------------------------------------------
# Gap 1: explicit directive survives a larger configured default
# ---------------------------------------------------------------------------


def test_prepare_submission_config_does_not_override_explicit_when_larger(
    tmp_path: Path,
) -> None:
    # Contract (PUBLIC_CONTRACTS §1 run-dir): config fills only absent directives.
    # nprocs 2 / maxcore 512 must survive even when defaults (32, 64) are larger.
    source = "! SP\n%pal nprocs 2 end\n%maxcore 512\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    inp = _write_inp(tmp_path, source)

    prepared = prepare_submission_resource_request(
        inp, inp.read_bytes(), default_max_cores=32, default_max_memory_gb=64
    )

    assert prepared.actions == ()
    # Independent: total_memory_gb=ceil(2*512/1024)=ceil(1.0)=1.
    assert prepared.resource_request == {"max_cores": 2, "max_memory_gb": 1}
    assert prepared.normalized_payload == source.encode("utf-8")


# ---------------------------------------------------------------------------
# Gap 2: MB-per-core floor and total-GB ceiling near the 1 GiB boundary
# ---------------------------------------------------------------------------


def test_maxcore_mb_per_core_floor_clamps_below_one() -> None:
    # 1 GB / 2048 cores: integer-divides to 0 MB; max(1,0) clamps to 1.
    assert maxcore_mb_per_core(max_memory_gb=1, max_cores=2048) == 1
    # 1 GB / 1 core: 1024 MB, well above floor.
    assert maxcore_mb_per_core(max_memory_gb=1, max_cores=1) == 1024
    # 0 GB clamped to 1 GB before division: 1024 // 4 = 256 MB.
    assert maxcore_mb_per_core(max_memory_gb=0, max_cores=4) == 256


@pytest.mark.parametrize(
    ("nprocs", "maxcore_mb", "expected_gb"),
    [
        (1, 1023, 1),  # ceil(1023/1024)=1 — just below 1 GiB
        (1, 1024, 1),  # ceil(1024/1024)=1 — exactly 1 GiB
        (1, 1025, 2),  # ceil(1025/1024)=2 — one MB above tips to 2 GB
        (2, 512, 1),  # 2*512=1024 MB: ceil(1.0)=1 GiB via two cores
        (2, 513, 2),  # 2*513=1026 MB: ceil(1026/1024)=2 GB just above
    ],
)
def test_resource_request_total_gb_ceiling_near_1024_mb(
    nprocs: int, maxcore_mb: int, expected_gb: int
) -> None:
    lines = [
        f"%pal nprocs {nprocs} end",
        f"%maxcore {maxcore_mb}",
        "* xyz 0 1",
        "H 0 0 0",
        "*",
    ]
    assert resource_request_from_lines(lines) == {
        "max_cores": nprocs,
        "max_memory_gb": expected_gb,
    }


# ---------------------------------------------------------------------------
# Gap 3: supplied-payload authority, disk immutability, and idempotency
# ---------------------------------------------------------------------------


def test_prepare_submission_supplied_payload_authority_and_idempotency(
    tmp_path: Path,
) -> None:
    # The on-disk file is not the source of truth: the caller passes bytes
    # explicitly.  Disk has nprocs 16 / maxcore 8192; supplied bytes carry
    # maxcore 513 but no %pal.  Only the supplied bytes drive normalization;
    # the disk file must be byte-identical afterward.  Re-preparing the
    # normalized payload (disk still conflicting) must produce no new actions.
    disk_text = "! SP\n%pal nprocs 16 end\n%maxcore 8192\n* xyz 0 1\nH 0 0 0\n*\n"
    inp = _write_inp(tmp_path, disk_text)
    supplied = b"! SP\n%maxcore 513\n* xyz 0 1\nH 0 0 0\n*\n"

    first = prepare_submission_resource_request(
        inp, supplied, default_max_cores=2, default_max_memory_gb=8
    )

    assert inp.read_bytes() == disk_text.encode("utf-8")
    assert first.actions == ("pal_nprocs_injected",)
    # Independent: nprocs=2 (default, no %pal in supplied); maxcore=513 (kept);
    # total_memory_gb=ceil(2*513/1024)=ceil(1026/1024)=ceil(1.002)=2.
    assert first.resource_request == {"max_cores": 2, "max_memory_gb": 2}
    assert first.normalized_payload == (
        b"! SP\n%maxcore 513\n%pal\n  nprocs 2\nend\n\n* xyz 0 1\nH 0 0 0\n*\n"
    )

    second = prepare_submission_resource_request(
        inp, first.normalized_payload, default_max_cores=2, default_max_memory_gb=8
    )

    assert second.actions == ()
    assert second.normalized_payload == first.normalized_payload
    assert second.resource_request == first.resource_request


# ---------------------------------------------------------------------------
# Gap 4: a %maxcore without a value is refused before normalization
# ---------------------------------------------------------------------------


def test_prepare_submission_bare_maxcore_is_rejected_not_duplicated(
    tmp_path: Path,
) -> None:
    # A bare "%maxcore" is a present directive whose value cannot be read.
    # Injecting the configured value beside it would leave two %maxcore lines
    # that the snapshot's duplicate check rejects; replacing it would guess the
    # requested memory. Preparation refuses it before any normalized bytes exist.
    source = "! SP\n%pal nprocs 4 end\n%maxcore\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    inp = _write_inp(tmp_path, source)
    # One such directive passes the duplicate check; only preparation refuses it.
    validate_unambiguous_orca_directives(source.splitlines(), label="rxn.inp")

    with pytest.raises(ValueError, match="%maxcore directive without a value"):
        prepare_submission_resource_request(
            inp, inp.read_bytes(), default_max_cores=4, default_max_memory_gb=8
        )

    assert inp.read_text(encoding="utf-8") == source


def test_prepare_submission_resource_request_rejects_a_lone_bare_maxcore(tmp_path: Path) -> None:
    # Injecting beside it produced ["%maxcore 2048", "%maxcore"]: two directives.
    inp = tmp_path / "rxn.inp"
    inp.write_bytes(b"%maxcore\n")

    with pytest.raises(ValueError, match="%maxcore directive without a value"):
        prepare_submission_resource_request(
            inp, inp.read_bytes(), default_max_cores=4, default_max_memory_gb=8
        )

    assert inp.read_bytes() == b"%maxcore\n"


@pytest.mark.parametrize(
    "line", ["%maxcore", "% maxcore", "%maxcore =512", "%maxcore-5", "%maxcore abc"]
)
def test_prepare_submission_resource_request_rejects_a_maxcore_without_a_value(
    tmp_path: Path, line: str
) -> None:
    source = f"! Opt\n%pal nprocs 4 end\n{line}\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    inp = _write_inp(tmp_path, source)
    # One such directive passes the duplicate check; preparation must refuse it.
    validate_unambiguous_orca_directives(source.splitlines(), label="rxn.inp")

    with pytest.raises(ValueError, match="%maxcore directive without a value"):
        prepare_submission_resource_request(
            inp, inp.read_bytes(), default_max_cores=4, default_max_memory_gb=8
        )

    assert inp.read_text(encoding="utf-8") == source


@pytest.mark.parametrize(
    ("head", "expected_head"),
    [
        ("! Opt\n# %maxcore\n", "! Opt\n%maxcore 2048\n# %maxcore\n"),
        ("! Opt # %maxcore\n", "! Opt # %maxcore\n%maxcore 2048\n"),
    ],
    ids=["comment-line", "route-comment"],
)
def test_prepare_submission_resource_request_injects_beside_a_commented_maxcore(
    tmp_path: Path, head: str, expected_head: str
) -> None:
    body = "%pal nprocs 4 end\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    inp = _write_inp(tmp_path, head + body)

    prepared = prepare_submission_resource_request(
        inp, inp.read_bytes(), default_max_cores=4, default_max_memory_gb=8
    )

    # Independent: maxcore_mb_per_core(8,4)=8192//4=2048, inserted after the
    # route line; total_memory_gb=ceil(4*2048/1024)=8. The comment stays.
    assert prepared.actions == ("maxcore_injected",)
    assert prepared.resource_request == {"max_cores": 4, "max_memory_gb": 8}
    assert prepared.normalized_payload == (expected_head + body).encode("utf-8")
    assert inp.read_text(encoding="utf-8") == head + body
    validate_unambiguous_orca_directives(
        prepared.normalized_payload.decode("utf-8").splitlines(), label="rxn.inp"
    )
