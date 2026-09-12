from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "pre-push"


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


@pytest.mark.parametrize("new_branch", [False, True])
@pytest.mark.parametrize(
    ("changed_file", "expected_skip_install"),
    [("extensions/workflows/pyproject.toml", "0"), ("README.md", "1")],
)
def test_pre_push_refreshes_dependencies_for_extension_metadata(
    tmp_path: Path,
    changed_file: str,
    expected_skip_install: str,
    new_branch: bool,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="Test User",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test User",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    _git(repo, env, "init", "-q")
    hook = repo / ".githooks" / "pre-push"
    hook.parent.mkdir()
    shutil.copy2(HOOK, hook)
    scripts = repo / "scripts"
    scripts.mkdir()
    # Observe the hook's install-step request without changing a real venv or
    # running the full suite. check.sh owns the actual pip invocation.
    (scripts / "check.sh").write_text(
        '#!/bin/sh\nprintf "skip_install=%s\\n" "$ORCA_AUTO_CHECK_SKIP_INSTALL"\n',
        encoding="utf-8",
    )
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    env["ORCA_AUTO_VENV"] = str(python.parent.parent)
    metadata = repo / "extensions" / "workflows" / "pyproject.toml"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('[project]\ndependencies = ["orca_auto==5.0.0"]\n', encoding="utf-8")
    (repo / "README.md").write_text("Initial documentation\n", encoding="utf-8")
    _git(repo, env, "add", ".")
    _git(repo, env, "commit", "-qm", "Initial fixture")
    base = _git(repo, env, "rev-parse", "HEAD")
    _git(repo, env, "update-ref", "refs/remotes/origin/main", base)

    changed = repo / changed_file
    changed.write_text(
        '[project]\ndependencies = ["orca_auto==5.0.1"]\n'
        if changed_file.endswith("pyproject.toml")
        else "Updated documentation\n",
        encoding="utf-8",
    )
    _git(repo, env, "add", changed_file)
    _git(repo, env, "commit", "-qm", "Update fixture")
    head = _git(repo, env, "rev-parse", "HEAD")
    remote_sha = "0" * 40 if new_branch else base
    assert _git(repo, env, "status", "--porcelain") == ""

    result = subprocess.run(
        ["bash", str(hook)],
        input=f"refs/heads/fixture {head} refs/heads/fixture {remote_sha}\n",
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"skip_install={expected_skip_install}\n" in result.stdout
