"""Guard and stage the matched release; never upload, overwrite, or repair a release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tomllib
from datetime import date
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from zipfile import ZipFile

PROJECTS = ("orca_auto", "orca_auto_workflows")
MAX_PYPI_RESPONSE = 1024 * 1024


def release_version(tag: str, commit: str) -> str:
    if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag):
        raise ValueError("release tag must be a stable vX.Y.Z version")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("release commit must be a full immutable Git SHA")
    return tag[1:]


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _changelog(repo: Path, version: str) -> str:
    contents = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = re.findall(
        rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})\n(.*?)(?=^## |\Z)",
        contents,
        re.M | re.S,
    )
    if len(sections) != 1:
        raise ValueError("release requires exactly one dated changelog section")
    released, notes = sections[0]
    date.fromisoformat(released)
    citation = (repo / "CITATION.cff").read_text(encoding="utf-8")
    if re.findall(r'^version: "([^"]+)"$', citation, re.M) != [version]:
        raise ValueError("citation version does not match release")
    if re.findall(r'^date-released: ["\']?(\d{4}-\d{2}-\d{2})["\']?$', citation, re.M) != [
        released
    ]:
        raise ValueError("citation release date does not match changelog")
    if not notes.strip():
        raise ValueError("release changelog section is empty")
    return notes.strip()


def guard(repo: Path, tag: str, commit: str) -> str:
    version = release_version(tag, commit)
    if _git(repo, "rev-parse", "HEAD") != commit:
        raise ValueError("checkout does not match selected commit")
    if _git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}") != commit:
        raise ValueError("tag does not match selected commit")
    _git(repo, "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main")
    if _git(repo, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("release source tree must be clean")
    core = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    workflows = tomllib.loads(
        (repo / "extensions/workflows/pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    if (core["name"], workflows["name"]) != PROJECTS:
        raise ValueError("unexpected release project names")
    if core["version"] != version or workflows["version"] != version:
        raise ValueError("tag and source versions differ")
    if core["optional-dependencies"]["workflows"] != [f"orca_auto_workflows=={version}"]:
        raise ValueError("core workflows extra must pin the release version")
    if workflows["dependencies"] != [f"orca_auto=={version}"]:
        raise ValueError("workflows must pin the core release version")
    _changelog(repo, version)
    return version


def filenames(version: str) -> tuple[str, ...]:
    return tuple(
        filename
        for project in PROJECTS
        for filename in (f"{project}-{version}-py3-none-any.whl", f"{project}-{version}.tar.gz")
    )


def _regular_files(directory: Path, expected: set[str]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"not a regular artifact directory: {directory}")
    entries = list(directory.iterdir())
    if {entry.name for entry in entries} != expected or any(
        entry.is_symlink() or not entry.is_file() for entry in entries
    ):
        raise ValueError(f"unexpected artifact file set in {directory}")


def _distribution_metadata(path: Path, project: str, version: str) -> None:
    if path.suffix == ".whl":
        with ZipFile(path) as wheel:
            names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError("wheel must contain one METADATA record")
            payload = wheel.read(names[0])
    else:
        with tarfile.open(path) as sdist:
            members = [
                member
                for member in sdist
                if len(Path(member.name).parts) == 2 and Path(member.name).name == "PKG-INFO"
            ]
            if len(members) != 1 or not members[0].isfile():
                raise ValueError("sdist must contain one root PKG-INFO record")
            contents = sdist.extractfile(members[0])
            if contents is None:
                raise ValueError("sdist metadata is unreadable")
            with contents:
                payload = contents.read()
    metadata = BytesParser().parsebytes(payload)
    if metadata.get_all("Name") != [project] or metadata.get_all("Version") != [version]:
        raise ValueError("distribution metadata does not match release")


def _checksums(digests: dict[str, str]) -> str:
    return "".join(f"{digests[name]}  {name}\n" for name in sorted(digests))


def stage(repo: Path, tag: str, commit: str, work: Path, output: Path) -> None:
    version = guard(repo, tag, commit)
    sources: list[Path] = []
    for directory, project in zip(("core-dist", "workflows-dist"), PROJECTS, strict=True):
        names = {name for name in filenames(version) if name.startswith(f"{project}-{version}-")}
        names.add(f"{project}-{version}.tar.gz")
        _regular_files(work / directory, names)
        for name in sorted(names):
            path = work / directory / name
            _distribution_metadata(path, project, version)
            sources.append(path)
    # Refuse reuse instead of replacing a prior run's evidence.
    output.mkdir(parents=True, exist_ok=False)
    (output / "dist").mkdir()
    for source in sources:
        shutil.copyfile(source, output / "dist" / source.name)
    digests = {
        name: hashlib.sha256((output / "dist" / name).read_bytes()).hexdigest()
        for name in filenames(version)
    }
    manifest = {"tag": tag, "version": version, "commit": commit, "sha256": digests}
    (output / "release.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "SHA256SUMS").write_text(_checksums(digests), encoding="utf-8")
    (output / "release-notes.md").write_text(
        f"## Motivation\n\nI publish ORCA_auto {version} core and optional workflows as a matched release.\n\n"
        f"## Changes\n\n{_changelog(repo, version)}\n\n"
        "## Verification\n\n"
        f"- Release commit: `{commit}` (`{tag}`).\n"
        "- Release gates: `make check`, isolated distribution installation profiles, and strict metadata checks.\n"
        "- GitHub publication requires a successful PyPI upload and matching remote artifact digests.\n"
        "- Publication does not install packages or restart local calculation workers.\n",
        encoding="utf-8",
    )


def verify_artifacts(artifacts: Path, tag: str, commit: str) -> dict[str, Any]:
    version = release_version(tag, commit)
    if artifacts.is_symlink() or not artifacts.is_dir():
        raise ValueError("release artifact bundle is not a regular directory")
    if {entry.name for entry in artifacts.iterdir()} != {
        "dist",
        "release.json",
        "SHA256SUMS",
        "release-notes.md",
    }:
        raise ValueError("unexpected release bundle contents")
    for name in ("release.json", "SHA256SUMS", "release-notes.md"):
        if (artifacts / name).is_symlink() or not (artifacts / name).is_file():
            raise ValueError("release bundle metadata must be regular files")
    _regular_files(artifacts / "dist", set(filenames(version)))
    manifest = json.loads((artifacts / "release.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != {"tag", "version", "commit", "sha256"}:
        raise ValueError("invalid release manifest")
    if (manifest["tag"], manifest["version"], manifest["commit"]) != (tag, version, commit):
        raise ValueError("artifact release identity differs from selected release")
    digests = manifest["sha256"]
    if not isinstance(digests, dict) or set(digests) != set(filenames(version)):
        raise ValueError("release manifest must describe exactly four distributions")
    for name, digest in digests.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid release digest")
        if hashlib.sha256((artifacts / "dist" / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"release checksum mismatch: {name}")
    if (artifacts / "SHA256SUMS").read_text(encoding="utf-8") != _checksums(digests):
        raise ValueError("SHA256SUMS does not match the release manifest")
    return manifest


def verify_remote_tag(repo: Path, tag: str, commit: str) -> None:
    release_version(tag, commit)
    ref = f"refs/tags/{tag}"
    refs = dict(
        (name, sha)
        for sha, name in (
            row.split()
            for row in _git(
                repo, "ls-remote", "--exit-code", "origin", ref, f"{ref}^{{}}"
            ).splitlines()
        )
    )
    if refs.get(f"{ref}^{{}}", refs.get(ref)) != commit:
        raise ValueError("remote tag no longer matches the selected release commit")


def select_project(artifacts: Path, tag: str, commit: str, project: str, output: Path) -> None:
    if project not in PROJECTS:
        raise ValueError("unknown release project")
    manifest = verify_artifacts(artifacts, tag, commit)
    version = manifest["version"]
    names = {f"{project}-{version}-py3-none-any.whl", f"{project}-{version}.tar.gz"}
    output.mkdir(parents=True, exist_ok=False)
    for name in sorted(names):
        target = output / name
        shutil.copyfile(artifacts / "dist" / name, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != manifest["sha256"][name]:
            raise ValueError(f"selected upload checksum mismatch: {name}")


def check_pypi(artifacts: Path, tag: str, commit: str) -> None:
    manifest = verify_artifacts(artifacts, tag, commit)
    version = manifest["version"]
    for project in PROJECTS:
        with urlopen(f"https://pypi.org/pypi/{project}/{version}/json", timeout=20) as response:
            payload = response.read(MAX_PYPI_RESPONSE + 1)
        if len(payload) > MAX_PYPI_RESPONSE:
            raise ValueError("PyPI response exceeds release verification limit")
        metadata = json.loads(payload)
        expected = {
            name: digest
            for name, digest in manifest["sha256"].items()
            if name in {f"{project}-{version}-py3-none-any.whl", f"{project}-{version}.tar.gz"}
        }
        urls = metadata["urls"]
        if not isinstance(urls, list) or len(urls) != 2:
            raise ValueError(f"PyPI does not expose the exact release pair for {project}")
        actual = {item["filename"]: item["digests"]["sha256"] for item in urls}
        if actual != expected:
            raise ValueError(f"PyPI filename/checksum mismatch for {project}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("guard", "stage", "verify", "pypi-check", "select-project"):
        command = commands.add_parser(name)
        command.add_argument("--tag", required=True)
        command.add_argument("--commit", required=True)
        if name in {"guard", "stage", "verify"}:
            command.add_argument("--repo", type=Path, default=Path.cwd())
        if name == "stage":
            command.add_argument("--work-dir", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
        elif name in {"verify", "pypi-check", "select-project"}:
            command.add_argument("--artifacts", type=Path, required=True)
        if name == "select-project":
            command.add_argument("--project", choices=PROJECTS, required=True)
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "guard":
        print(guard(args.repo, args.tag, args.commit))
    elif args.command == "stage":
        stage(args.repo, args.tag, args.commit, args.work_dir, args.output)
    elif args.command == "verify":
        verify_artifacts(args.artifacts, args.tag, args.commit)
        verify_remote_tag(args.repo, args.tag, args.commit)
    elif args.command == "select-project":
        select_project(args.artifacts, args.tag, args.commit, args.project, args.output)
    else:
        check_pypi(args.artifacts, args.tag, args.commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
