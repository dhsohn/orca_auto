"""Release metadata stays in lockstep across its three declarations.

CITATION.cff drifted behind pyproject in 0.2.0, 0.2.1, and 0.3.0 because the
release checklist never mentioned it. The checklist now does, and this test
makes the drift a gate failure instead of a checklist miss.

A fourth declaration lives outside the source tree: the installed distribution
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

from orca_auto import _version

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
