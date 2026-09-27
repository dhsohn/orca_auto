from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.resource_directives import (
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
    text = prepared.normalized_payload.decode("utf-8")

    assert prepared.resource_request == {"max_cores": 8, "max_memory_gb": 32}
    assert prepared.actions == ("pal_nprocs_injected", "maxcore_injected")
    assert inp.read_text(encoding="utf-8") == source
    assert "%pal" in text
    assert "nprocs 8" in text
    assert "%maxcore 4096" in text


def test_prepare_submission_resource_request_preserves_existing_nprocs(tmp_path: Path) -> None:
    inp = _write_inp(tmp_path, "! Opt\n%pal\n  nprocs 12\nend\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")

    prepared = prepare_submission_resource_request(
        inp, inp.read_bytes(), default_max_cores=8, default_max_memory_gb=32
    )
    text = prepared.normalized_payload.decode("utf-8")

    assert prepared.resource_request == {"max_cores": 12, "max_memory_gb": 32}
    assert prepared.actions == ("maxcore_injected",)
    assert "nprocs 12" in text
    assert "%maxcore 2730" in text


def test_prepare_submission_resource_request_honors_pal_route_shorthand(tmp_path: Path) -> None:
    # "! Opt PAL4" already requests 4 processes via ORCA's route shorthand, so
    # no conflicting %pal nprocs block should be injected and the resource
    # request must reflect 4 cores (not the default_max_cores).
    inp = _write_inp(tmp_path, "! Opt PAL4\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")

    prepared = prepare_submission_resource_request(
        inp, inp.read_bytes(), default_max_cores=8, default_max_memory_gb=32
    )
    text = prepared.normalized_payload.decode("utf-8")

    assert prepared.resource_request["max_cores"] == 4
    assert "pal_nprocs_injected" not in prepared.actions
    assert "%pal" not in text


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
