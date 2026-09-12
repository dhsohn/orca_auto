"""Matched distributions and citation metadata describe the same release state.

CITATION.cff drifted behind pyproject in 0.2.0, 0.2.1, and 0.3.0 because the
release checklist never mentioned it. The checklist now does, and this test
makes the drift a gate failure instead of a checklist miss.

Another declaration lives outside the source tree: the installed distribution
metadata a deployment reports. These tests cover the detector that compares it
against the checkout; they deliberately do not assert parity for the
environment running them, because the check script reinstalls the package
immediately before pytest and would make that assertion true by construction.
`orca_auto service status` carries the live gate instead.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

from orca_auto import _version

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


def test_workflows_distribution_version_matches_core() -> None:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        core = tomllib.load(fh)["project"]
    with (_REPO_ROOT / "extensions" / "workflows" / "pyproject.toml").open("rb") as fh:
        project = tomllib.load(fh)["project"]
    assert project["name"] == "orca_auto_workflows"
    assert project["version"] == core["version"]
    assert core["optional-dependencies"]["workflows"] == [f"orca_auto_workflows=={core['version']}"]
    assert project["dependencies"] == [f"orca_auto=={core['version']}"]


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


def _write_pyproject(root: Path, *, name: str = "orca_auto", version: str = "1.2.3") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n', encoding="utf-8"
    )
    return root


def test_installed_version_drift_reports_a_stale_editable_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The 2026-08-02 live shape: metadata frozen at 0.1.0 by a June install
    # while the checkout it imports had moved to 1.0.0.
    root = _write_pyproject(tmp_path / "checkout", version="1.0.0")
    monkeypatch.setattr(_version, "package_version", lambda: "0.1.0")

    assert _version.installed_version_drift(root) == ("0.1.0", "1.0.0")


def test_installed_version_drift_is_silent_when_the_versions_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write_pyproject(tmp_path / "checkout", version="1.0.0")
    monkeypatch.setattr(_version, "package_version", lambda: "1.0.0")

    assert _version.installed_version_drift(root) is None


def test_installed_version_drift_returns_no_verdict_without_a_source_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A wheel install has no pyproject to compare against; that is a different
    # deployment shape, not drift.
    monkeypatch.setattr(_version, "package_version", lambda: "0.1.0")

    assert _version.installed_version_drift(tmp_path / "empty") is None


def test_installed_version_drift_reads_this_checkout_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Faking only the installed side leaves the default source-root derivation
    # and the project-name literal under test. Without this, an off-by-one in
    # `parents[...]` or a PEP 503 rename to `orca-auto` would leave every
    # deployment reporting "no verdict" forever while the suite stayed green.
    monkeypatch.setattr(_version, "package_version", lambda: "0.0.0+stale")

    assert _version.installed_version_drift() == ("0.0.0+stale", _pyproject_version())


def test_installed_version_drift_ignores_an_unrelated_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write_pyproject(tmp_path / "other", name="something_else", version="9.9.9")
    monkeypatch.setattr(_version, "package_version", lambda: "1.0.0")

    assert _version.installed_version_drift(root) is None
