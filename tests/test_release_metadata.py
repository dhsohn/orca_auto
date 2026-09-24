"""Distribution and citation metadata describe the same release state.

CITATION.cff drifted behind pyproject in 0.2.0, 0.2.1, and 0.3.0 because the
release checklist never mentioned it. The checklist now does, and this test
makes the drift a gate failure instead of a checklist miss.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with open(_REPO_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _assert_release_metadata(version: str, citation: str, changelog: str) -> None:
    match = re.search(r'^version: "(?P<version>[^"]+)"$', citation, re.M)
    assert match is not None, "CITATION.cff must declare a quoted version"
    assert match.group("version") == version
    headings = re.findall(
        rf"^## \[{re.escape(version)}\] - (Unreleased|\d{{4}}-\d{{2}}-\d{{2}})$",
        changelog,
        re.M,
    )
    assert len(headings) == 1, f"CHANGELOG.md must contain one section for {version}"
    citation_fields = yaml.safe_load(citation)
    assert isinstance(citation_fields, dict), "CITATION.cff must contain a YAML mapping"
    if re.search(r"\.dev\d+(?:\+.*)?$", version):
        assert headings[0] == "Unreleased", "Development versions must remain unreleased"
        assert "date-released" not in citation_fields, (
            "Development citations must not invent a release date"
        )
    else:
        assert headings[0] != "Unreleased", "Release versions require a dated changelog"
        date = citation_fields.get("date-released")
        assert date is not None, "Released citations require a release date"
        assert str(date) == headings[0], "Citation and changelog release dates differ"


def test_release_metadata_matches_current_source() -> None:
    citation = (_REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8")
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    _assert_release_metadata(_pyproject_version(), citation, changelog)


@pytest.mark.parametrize(
    ("version", "heading", "date", "accepted"),
    [
        ("5.0.0.dev0", "Unreleased", "", True),
        ("5.0.0.dev0", "2026-09-12", "2026-09-12", False),
        ("5.0.0.dev0", "Unreleased", "2026-09-12", False),
        ("5.0.0", "2026-09-12", "2026-09-12", True),
        ("5.0.0", "Unreleased", "", False),
        ("5.0.0", "2026-09-12", "", False),
        ("5.0.0", "2026-09-12", "2026-09-11", False),
    ],
)
def test_release_state_gate_rejects_invented_or_missing_release_dates(
    version: str, heading: str, date: str, accepted: bool
) -> None:
    citation = f'version: "{version}"\n'
    if date:
        citation += f'date-released: "{date}"\n'
    changelog = f"## [{version}] - {heading}\n"
    if accepted:
        _assert_release_metadata(version, citation, changelog)
    else:
        with pytest.raises(AssertionError):
            _assert_release_metadata(version, citation, changelog)


@pytest.mark.parametrize(
    "date_field",
    [
        'date-released: "2026-09-12"',
        "date-released: 2026-09-12",
        '"date-released": 2026-09-12',
        "date-released: null",
    ],
)
def test_development_citation_rejects_any_release_date_field(date_field: str) -> None:
    citation = f'version: "5.0.0.dev0"\n{date_field}\n'
    with pytest.raises(AssertionError, match="Development citations"):
        _assert_release_metadata("5.0.0.dev0", citation, "## [5.0.0.dev0] - Unreleased\n")
