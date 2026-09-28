from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.input_artifacts import xyzfile_input_path


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["! SP", "* xyzfile 0 1 geom.xyz"], "/job/geom.xyz"),
        (["! SP", '* xyzfile 0 1 "my geom.xyz" # start'], "/job/my geom.xyz"),
        (["! SP", "* xyzfile 0 1 /abs/geom.xyz"], "/abs/geom.xyz"),
        (["! SP", "* xyz 0 1", "H 0 0 0", "*"], ""),
        # The first geometry block decides, as for binding and validation.
        (["! SP", "* xyz 0 1", "H 0 0 0", "*", "* xyzfile 0 1 later.xyz"], ""),
    ],
)
def test_xyzfile_input_path_reads_the_first_geometry_block(lines: list[str], expected: str) -> None:
    assert xyzfile_input_path(lines, Path("/job")) == expected
