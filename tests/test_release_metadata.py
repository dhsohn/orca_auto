"""Release metadata stays in lockstep across its three declarations.

CITATION.cff drifted behind pyproject in 0.2.0, 0.2.1, and 0.3.0 because the
release checklist never mentioned it. The checklist now does, and this test
makes the drift a gate failure instead of a checklist miss.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with open(_REPO_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_citation_version_matches_pyproject() -> None:
    citation = (_REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8")
    match = re.search(r'^version: "(?P<version>[^"]+)"$', citation, re.M)
    assert match is not None, "CITATION.cff must declare a quoted version"
    assert match.group("version") == _pyproject_version()


def test_changelog_has_section_for_current_version() -> None:
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version = _pyproject_version()
    assert re.search(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.M
    ), f"CHANGELOG.md must contain a dated section for {version}"
