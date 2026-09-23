from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
import yaml

from scripts import release

TAG = "v1.2.3"
VERSION = "1.2.3"
ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "release fixture")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Release Tests")
    _git(repo, "config", "user.email", "release-tests@example.invalid")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "orca_auto"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    (repo / "CITATION.cff").write_text(
        'version: "1.2.3"\ndate-released: 2026-09-13\n', encoding="utf-8"
    )
    (repo / "CHANGELOG.md").write_text(
        "## [1.2.3] - 2026-09-13\n\n### Changed\n\n- Verified fixture.\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    _git(repo, "tag", "-a", TAG, "-m", "annotated fixture release")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "main", "--tags")
    return repo, commit


def _retag(repo: Path) -> str:
    commit = _commit(repo)
    _git(repo, "tag", "-f", TAG)
    return commit


def _build_fixture(work: Path, *, metadata_version: str = VERSION) -> None:
    for directory, project in zip(("core-dist",), release.PROJECTS, strict=True):
        output = work / directory
        output.mkdir(parents=True)
        payload = f"Metadata-Version: 2.1\nName: {project}\nVersion: {metadata_version}\n".encode()
        with ZipFile(output / f"{project}-{VERSION}-py3-none-any.whl", "w") as wheel:
            wheel.writestr(f"{project}-{VERSION}.dist-info/METADATA", payload)
        with tarfile.open(output / f"{project}-{VERSION}.tar.gz", "w:gz") as sdist:
            member = tarfile.TarInfo(f"{project}-{VERSION}/PKG-INFO")
            member.size = len(payload)
            sdist.addfile(member, io.BytesIO(payload))


@pytest.fixture
def bundle(repository: tuple[Path, str], tmp_path: Path) -> tuple[Path, Path, str]:
    repo, commit = repository
    work = tmp_path / "package-check"
    _build_fixture(work)
    artifacts = tmp_path / "artifacts"
    release.stage(repo, TAG, commit, work, artifacts)
    return artifacts, repo, commit


@pytest.mark.parametrize(
    "tag",
    ["1.2.3", "v1.2", "v01.2.3", "v1.2.3.dev0", "v1.2.3rc1", "v1.2.3+local", "v1.2.3\n"],
)
def test_release_tag_guard_rejects_nonstable_or_noncanonical_versions(tag: str) -> None:
    with pytest.raises(ValueError, match="stable vX.Y.Z"):
        release.release_version(tag, "a" * 40)


@pytest.mark.parametrize("commit", ["main", "abcd123", "A" * 40, "a" * 40 + "\n"])
def test_release_commit_guard_rejects_mutable_or_malformed_refs(commit: str) -> None:
    with pytest.raises(ValueError, match="immutable Git SHA"):
        release.release_version(TAG, commit)


def test_git_guard_accepts_annotated_tag_on_main(repository: tuple[Path, str]) -> None:
    repo, commit = repository
    assert _git(repo, "rev-parse", f"refs/tags/{TAG}") != commit
    assert release.guard(repo, TAG, commit) == VERSION
    release.verify_remote_tag(repo, TAG, commit)


def test_git_guard_accepts_lightweight_tag_and_later_main_commit(
    repository: tuple[Path, str],
) -> None:
    repo, commit = repository
    _git(repo, "tag", "-f", TAG)
    (repo / "later.txt").write_text("later main commit", encoding="utf-8")
    _commit(repo)
    _git(repo, "push", "origin", "main")
    _git(repo, "push", "--force", "origin", TAG)
    _git(repo, "checkout", "--detach", commit)
    assert release.guard(repo, TAG, commit) == VERSION
    release.verify_remote_tag(repo, TAG, commit)


def test_git_guard_rejects_commit_outside_origin_main(repository: tuple[Path, str]) -> None:
    repo, _ = repository
    _git(repo, "checkout", "-b", "unmerged")
    (repo / "unmerged.txt").write_text("unmerged", encoding="utf-8")
    commit = _retag(repo)
    with pytest.raises(subprocess.CalledProcessError):
        release.guard(repo, TAG, commit)


def test_git_guard_rejects_local_tag_left_on_older_reachable_commit(
    repository: tuple[Path, str],
) -> None:
    repo, older_commit = repository
    (repo / "later.txt").write_text("selected release commit", encoding="utf-8")
    selected_commit = _commit(repo)
    _git(repo, "push", "origin", "main")

    assert _git(repo, "rev-parse", "HEAD") == selected_commit
    assert _git(repo, "rev-parse", "refs/remotes/origin/main") == selected_commit
    assert _git(repo, "rev-parse", f"refs/tags/{TAG}^{{commit}}") == older_commit
    with pytest.raises(ValueError, match="tag does not match selected commit"):
        release.guard(repo, TAG, selected_commit)


def test_git_guard_rejects_dirty_or_wrong_checkout(repository: tuple[Path, str]) -> None:
    repo, commit = repository
    with pytest.raises(ValueError, match="checkout"):
        release.guard(repo, TAG, "a" * 40)
    (repo / "untracked.txt").write_text("not released", encoding="utf-8")
    with pytest.raises(ValueError, match="clean"):
        release.guard(repo, TAG, commit)


@pytest.mark.parametrize(
    "path,old,new,message",
    [
        ("pyproject.toml", 'version = "1.2.3"', 'version = "1.2.3.dev0"', "source versions"),
        ("pyproject.toml", 'name = "orca_auto"', 'name = "other"', "project name"),
        ("CHANGELOG.md", "2026-09-13", "Unreleased", "dated changelog"),
        ("CITATION.cff", "2026-09-13", "2026-09-12", "release date"),
    ],
)
def test_source_metadata_guards_fail_closed(
    repository: tuple[Path, str], path: str, old: str, new: str, message: str
) -> None:
    repo, _ = repository
    target = repo / path
    target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    commit = _retag(repo)
    _git(repo, "push", "origin", "main")
    with pytest.raises(ValueError, match=message):
        release.guard(repo, TAG, commit)


def test_remote_tag_guard_rejects_tag_moved_after_build(repository: tuple[Path, str]) -> None:
    repo, commit = repository
    (repo / "later.txt").write_text("tag moved", encoding="utf-8")
    _retag(repo)
    _git(repo, "push", "--force", "origin", TAG)
    with pytest.raises(ValueError, match="remote tag"):
        release.verify_remote_tag(repo, TAG, commit)


def test_staged_bundle_contains_only_checked_distributions_and_flat_download_checksums(
    bundle: tuple[Path, Path, str], tmp_path: Path
) -> None:
    artifacts, _, commit = bundle
    manifest = release.verify_artifacts(artifacts, TAG, commit)
    assert set(manifest["sha256"]) == set(release.filenames(VERSION))
    for line in (artifacts / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        assert line.split()[1] in release.filenames(VERSION)
    subprocess.run(
        ["sha256sum", "--check", str(artifacts / "SHA256SUMS")],
        cwd=artifacts / "dist",
        check=True,
        capture_output=True,
    )
    assert "## Motivation" in (artifacts / "release-notes.md").read_text(encoding="utf-8")
    assert "## Changes" in (artifacts / "release-notes.md").read_text(encoding="utf-8")
    assert "## Verification" in (artifacts / "release-notes.md").read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        release.stage(
            artifacts.parent / "source", TAG, commit, tmp_path / "package-check", artifacts
        )


@pytest.mark.parametrize("project", release.PROJECTS)
def test_select_project_cli_copies_only_exact_project_pair(
    bundle: tuple[Path, Path, str], tmp_path: Path, project: str
) -> None:
    artifacts, _, commit = bundle
    output = tmp_path / "upload-dist"

    assert (
        release.main(
            [
                "select-project",
                "--tag",
                TAG,
                "--commit",
                commit,
                "--artifacts",
                str(artifacts),
                "--project",
                project,
                "--output",
                str(output),
            ]
        )
        == 0
    )

    expected = {f"{project}-{VERSION}-py3-none-any.whl", f"{project}-{VERSION}.tar.gz"}
    assert {path.name for path in output.iterdir()} == expected
    for name in expected:
        assert (output / name).read_bytes() == (artifacts / "dist" / name).read_bytes()
    release.verify_artifacts(artifacts, TAG, commit)


@pytest.mark.parametrize("project", ["orca", "orca_auto_extra", "orca-auto", "../orca_auto"])
def test_select_project_refuses_unknown_project_before_creating_output(
    bundle: tuple[Path, Path, str], tmp_path: Path, project: str
) -> None:
    artifacts, _, commit = bundle
    output = tmp_path / "unknown-project"
    with pytest.raises(ValueError, match="unknown release project"):
        release.select_project(artifacts, TAG, commit, project, output)
    assert not output.exists()


@pytest.mark.parametrize("occupied", [False, True])
def test_select_project_refuses_reused_output_without_modifying_it(
    bundle: tuple[Path, Path, str], tmp_path: Path, occupied: bool
) -> None:
    artifacts, _, commit = bundle
    output = tmp_path / "already-used"
    output.mkdir()
    if occupied:
        (output / "existing.whl").write_bytes(b"prior release evidence")
    before = {path.name: path.read_bytes() for path in output.iterdir()}

    with pytest.raises(FileExistsError):
        release.select_project(artifacts, TAG, commit, "orca_auto", output)

    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


@pytest.mark.parametrize("tampered_project", release.PROJECTS)
def test_select_project_checks_full_bundle_even_when_other_project_is_tampered(
    bundle: tuple[Path, Path, str], tmp_path: Path, tampered_project: str
) -> None:
    artifacts, _, commit = bundle
    (artifacts / "dist" / f"{tampered_project}-{VERSION}.tar.gz").write_bytes(b"tampered")
    output = tmp_path / "must-not-upload"

    with pytest.raises(ValueError, match="checksum mismatch"):
        release.select_project(artifacts, TAG, commit, "orca_auto", output)

    assert not output.exists()


def test_select_project_rejects_corruption_during_copy(
    bundle: tuple[Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts, _, commit = bundle
    copyfile = release.shutil.copyfile

    def corrupt_copy(source: Path, target: Path) -> None:
        copyfile(source, target)
        target.write_bytes(b"corrupted during copy")

    monkeypatch.setattr(release.shutil, "copyfile", corrupt_copy)
    with pytest.raises(ValueError, match="selected upload checksum mismatch"):
        release.select_project(artifacts, TAG, commit, "orca_auto", tmp_path / "corrupt-upload")


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "tampered", "symlink", "checksum", "manifest"]
)
def test_artifact_verifier_rejects_changed_file_set_or_checksums(
    bundle: tuple[Path, Path, str], mutation: str
) -> None:
    artifacts, _, commit = bundle
    target = artifacts / "dist" / release.filenames(VERSION)[0]
    if mutation == "missing":
        target.unlink()
    elif mutation == "extra":
        (artifacts / "dist" / "unexpected.whl").write_bytes(b"unverified")
    elif mutation == "tampered":
        target.write_bytes(b"changed")
    elif mutation == "symlink":
        target.unlink()
        target.symlink_to(artifacts / "release.json")
    elif mutation == "checksum":
        (artifacts / "SHA256SUMS").write_text("wrong\n", encoding="utf-8")
    else:
        manifest = json.loads((artifacts / "release.json").read_text(encoding="utf-8"))
        manifest["commit"] = "b" * 40
        (artifacts / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        release.verify_artifacts(artifacts, TAG, commit)


@pytest.mark.parametrize("mutation", ["missing", "extra", "metadata"])
def test_staging_rejects_wrong_distribution_set_before_creating_output(
    repository: tuple[Path, str], tmp_path: Path, mutation: str
) -> None:
    repo, commit = repository
    work = tmp_path / "build"
    _build_fixture(work, metadata_version="1.2.2" if mutation == "metadata" else VERSION)
    if mutation == "missing":
        (work / "core-dist" / release.filenames(VERSION)[0]).unlink()
    elif mutation == "extra":
        (work / "core-dist" / "unverified.whl").write_bytes(b"extra")
    output = tmp_path / "bad-stage"
    with pytest.raises(ValueError):
        release.stage(repo, TAG, commit, work, output)
    assert not output.exists()


@pytest.mark.parametrize("mutation", ["none", "digest", "missing", "extra", "oversize", "network"])
def test_pypi_readback_is_bounded_and_requires_exact_published_pair(
    bundle: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    artifacts, _, commit = bundle
    manifest = release.verify_artifacts(artifacts, TAG, commit)
    calls: list[tuple[str, int]] = []

    def response(url: str, *, timeout: int) -> io.BytesIO:
        calls.append((url, timeout))
        if mutation == "network":
            raise OSError("index unavailable")
        if mutation == "oversize":
            return io.BytesIO(b"x" * (release.MAX_PYPI_RESPONSE + 1))
        project = url.split("/")[-3]
        urls: list[dict[str, Any]] = [
            {"filename": name, "digests": {"sha256": manifest["sha256"][name]}}
            for name in release.filenames(VERSION)
            if name.startswith(f"{project}-{VERSION}")
        ]
        if mutation == "digest":
            urls[0]["digests"]["sha256"] = "0" * 64
        elif mutation == "missing":
            urls.pop()
        elif mutation == "extra":
            urls.append(urls[0])
        return io.BytesIO(json.dumps({"urls": urls}).encode())

    monkeypatch.setattr(release, "urlopen", response)
    if mutation == "none":
        release.check_pypi(artifacts, TAG, commit)
        assert calls == [
            (f"https://pypi.org/pypi/{project}/{VERSION}/json", 20) for project in release.PROJECTS
        ]
    else:
        with pytest.raises((ValueError, OSError)):
            release.check_pypi(artifacts, TAG, commit)
        assert len(calls) == 1


def test_release_workflow_has_one_trigger_and_scoped_publication_permissions() -> None:
    # BaseLoader intentionally retains YAML's `on` key instead of YAML1.1 bool coercion.
    workflow = yaml.load(
        (ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert set(jobs) == {"build", "publish-pypi", "github-release"}
    assert {name: job["timeout-minutes"] for name, job in jobs.items()} == {
        "build": "30",
        "publish-pypi": "10",
        "github-release": "10",
    }
    assert jobs["build"].get("permissions", workflow["permissions"]) == {"contents": "read"}
    assert jobs["publish-pypi"]["permissions"] == {"contents": "read", "id-token": "write"}
    publisher = jobs["publish-pypi"]
    assert publisher["environment"] == {
        "name": "pypi",
        "url": "https://pypi.org/project/orca_auto/",
    }
    assert publisher["env"] == {"RELEASE_PROJECT": "orca_auto"}
    assert "strategy" not in publisher
    assert jobs["publish-pypi"]["needs"] == "build"
    assert jobs["github-release"]["permissions"] == {"contents": "write"}
    assert jobs["github-release"]["needs"] == ["build", "publish-pypi"]
    assert "github.repository == 'dhsohn/orca_auto'" in jobs["build"]["if"]
    for name, job in jobs.items():
        assert "continue-on-error" not in job
        if name != "build":
            # Publication relies on the default success condition of every need.
            assert "if" not in job
        for step in job["steps"]:
            assert "continue-on-error" not in step
            assert "always(" not in "".join(step.get("if", "").lower().split())


def test_release_workflow_publishes_only_verified_same_run_artifacts() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader
    )
    jobs = workflow["jobs"]
    build_commands = "\n".join(step.get("run", "") for step in jobs["build"]["steps"])
    assert "make check" in build_commands
    assert "scripts.check_distributions --work-dir" in build_commands
    assert "twine check --strict" in build_commands
    assert jobs["build"]["outputs"] == {"artifact-id": "${{ steps.upload.outputs.artifact-id }}"}
    uploads = [
        step
        for step in jobs["build"]["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    assert uploads[0]["id"] == "upload"
    for job in jobs.values():
        for step in job["steps"]:
            if "uses" in step:
                assert len(step["uses"].split("@")[1]) == 40
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["ref"] == "${{ github.sha }}"
                assert step["with"]["persist-credentials"] == "false"
    for job_name in ("publish-pypi", "github-release"):
        steps = jobs[job_name]["steps"]
        downloads = [
            step for step in steps if step.get("uses", "").startswith("actions/download-artifact@")
        ]
        assert len(downloads) == 1
        assert downloads[0]["with"]["artifact-ids"] == "${{ needs.build.outputs.artifact-id }}"
        assert downloads[0]["with"]["digest-mismatch"] == "error"
        verifications = [step for step in steps if "release.py verify" in step.get("run", "")]
        assert len(verifications) == 1
        assert "if" not in downloads[0]
        assert "if" not in verifications[0]
        writes = [
            step
            for step in steps
            if step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
            or "gh release create" in step.get("run", "")
        ]
        assert len(writes) == 1
        assert steps.index(downloads[0]) < steps.index(verifications[0]) < steps.index(writes[0])
        if job_name == "publish-pypi":
            selections = [
                step for step in steps if "release.py select-project" in step.get("run", "")
            ]
            assert len(selections) == 1
            assert "if" not in selections[0]
            assert (
                steps.index(downloads[0])
                < steps.index(selections[0])
                < steps.index(verifications[0])
            )
            assert selections[0]["run"] == (
                'python3 scripts/release.py select-project --tag "$RELEASE_TAG" --commit "$RELEASE_COMMIT" '
                '--artifacts release-artifacts --project "$RELEASE_PROJECT" --output upload-dist'
            )
        commands = "\n".join(step.get("run", "") for step in steps)
        assert "pip install" not in commands and "make check" not in commands
        assert "release.py verify" in commands
    publish = jobs["publish-pypi"]["steps"][-1]
    assert publish["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert publish["with"] == {
        "packages-dir": "upload-dist/",
        "skip-existing": "false",
        "verify-metadata": "true",
        "attestations": "true",
    }
    release_steps = jobs["github-release"]["steps"]
    assert "release.py pypi-check" in release_steps[-2]["run"]
    assert "release.py verify" in release_steps[-2]["run"]
    command = release_steps[-1]["run"]
    assert "--verify-tag" in command
    assert "--clobber" not in command
    assert "release-artifacts/dist/* release-artifacts/SHA256SUMS" in command
